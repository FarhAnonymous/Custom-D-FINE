import torch 
import torch.nn as nn 

from datetime import datetime
from pathlib import Path 
from typing import Dict
import atexit

from ..misc import dist_utils
from ..core import BaseConfig


def to(m: nn.Module, device: str):
    if m is None:
        return None 
    return m.to(device) 


def remove_module_prefix(state_dict):
    new_state_dict = {}
    for k, v in state_dict.items():
        if k.startswith('module.'):
            new_state_dict[k[7:]] = v
        else:
            new_state_dict[k] = v
    return new_state_dict


class BaseSolver(object):
    def __init__(self, cfg: BaseConfig) -> None:
        self.cfg = cfg
        self.obj365_ids = [
            0, 46, 5, 58, 114, 55, 116, 65, 21, 40, 176, 127, 249, 24, 56, 139, 92, 78, 99, 96, 
            144, 295, 178, 180, 38, 39, 13, 43, 120, 219, 148, 173, 165, 154, 137, 113, 145, 146, 
            204, 8, 35, 10, 88, 84, 93, 26, 112, 82, 265, 104, 141, 152, 234, 143, 150, 97, 2, 
            50, 25, 75, 98, 153, 37, 73, 115, 132, 106, 61, 163, 134, 277, 81, 133, 18, 94, 30, 
            169, 70, 328, 226
        ]
    def _setup(self):
        """Avoid instantiating unnecessary classes"""
        cfg = self.cfg
        if cfg.device:
            device = torch.device(cfg.device)
        else:
            device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        self.model = cfg.model

        # NOTE: Must load_tuning_state before EMA instance building
        if self.cfg.tuning:
            print(f'Tuning checkpoint from {self.cfg.tuning}')
            self.load_tuning_state(self.cfg.tuning)

        self.model = dist_utils.warp_model(
            self.model.to(device), sync_bn=cfg.sync_bn, find_unused_parameters=cfg.find_unused_parameters
        )

        self.criterion = self.to(cfg.criterion, device)
        self.postprocessor = self.to(cfg.postprocessor, device)

        self.ema = self.to(cfg.ema, device)
        self.scaler = cfg.scaler

        self.device = device
        self.last_epoch = self.cfg.last_epoch

        self.output_dir = Path(cfg.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.writer = cfg.writer

        if self.writer:
            atexit.register(self.writer.close)
            if dist_utils.is_main_process():
                self.writer.add_text(f'config', '{:s}'.format(cfg.__repr__()), 0)

    def cleanup(self):
        if self.writer:
            atexit.register(self.writer.close)

    def train(self):
        self._setup()
        self.optimizer = self.cfg.optimizer
        self.lr_scheduler = self.cfg.lr_scheduler
        self.lr_warmup_scheduler = self.cfg.lr_warmup_scheduler

        self.train_dataloader = dist_utils.warp_loader(
            self.cfg.train_dataloader, shuffle=self.cfg.train_dataloader.shuffle
        )
        self.val_dataloader = dist_utils.warp_loader(
            self.cfg.val_dataloader, shuffle=self.cfg.val_dataloader.shuffle
        )

        self.evaluator = self.cfg.evaluator

        # NOTE: Instantiating order
        if self.cfg.resume:
            print(f'Resume checkpoint from {self.cfg.resume}')
            self.load_resume_state(self.cfg.resume)

    def eval(self):
        self._setup()

        self.val_dataloader = dist_utils.warp_loader(
            self.cfg.val_dataloader, shuffle=self.cfg.val_dataloader.shuffle
        )

        self.evaluator = self.cfg.evaluator

        if self.cfg.resume:
            print(f'Resume checkpoint from {self.cfg.resume}')
            self.load_resume_state(self.cfg.resume)

    def to(self, module, device):
        return module.to(device) if hasattr(module, 'to') else module

    def state_dict(self):
        """State dict, train/eval"""
        state = {}
        state['date'] = datetime.now().isoformat()

        # For resume
        state['last_epoch'] = self.last_epoch

        for k, v in self.__dict__.items():
            if hasattr(v, 'state_dict'):
                v = dist_utils.de_parallel(v)
                state[k] = v.state_dict()

        return state

    def load_state_dict(self, state):
        """Load state dict, train/eval"""
        if 'last_epoch' in state:
            self.last_epoch = state['last_epoch']
            print('Load last_epoch')

        for k, v in self.__dict__.items():
            if hasattr(v, 'load_state_dict') and k in state:
                v = dist_utils.de_parallel(v)
                v.load_state_dict(state[k])
                print(f'Load {k}.state_dict')

            if hasattr(v, 'load_state_dict') and k not in state:
                if k == 'ema':
                    model = getattr(self, 'model', None)
                    if model is not None:
                        ema = dist_utils.de_parallel(v)
                        model_state_dict = remove_module_prefix(model.state_dict())
                        ema.load_state_dict({'module': model_state_dict})
                        print(f'Load {k}.state_dict from model.state_dict')
                else:
                    print(f'Not load {k}.state_dict')

    def load_resume_state(self, path: str):
        """Load resume"""
        if path.startswith('http'):
            state = torch.hub.load_state_dict_from_url(path, map_location='cpu')
        else:
            state = torch.load(path, map_location='cpu')
        
        # state['model'] = remove_module_prefix(state['model'])
        self.load_state_dict(state)

    def load_tuning_state(self, path: str):
        """Load model for tuning and adjust mismatched head parameters"""
        if path.startswith('http'):
            state = torch.hub.load_state_dict_from_url(path, map_location='cpu')
        else:
            state = torch.load(path, map_location='cpu')

        module = dist_utils.de_parallel(self.model)

        # Load the appropriate state dict
        if 'ema' in state:
            pretrain_state_dict = state['ema']['module']
        else:
            pretrain_state_dict = state['model']

        # Adjust head parameters between datasets
        try:
            adjusted_state_dict = self._adjust_head_parameters(module.state_dict(), pretrain_state_dict)
            stat, infos = self._matched_state(module.state_dict(), adjusted_state_dict)
        except:
            stat, infos = self._matched_state(module.state_dict(), pretrain_state_dict)

        module.load_state_dict(stat, strict=False)
        print(f'Load model.state_dict, {infos}')

    @staticmethod
    def _matched_state(state: Dict[str, torch.Tensor], params: Dict[str, torch.Tensor]):
        missed_list = []
        unmatched_list = []
        matched_state = {}
        for k, v in state.items():
            if k in params:
                if v.shape == params[k].shape:
                    matched_state[k] = params[k]
                else:
                    unmatched_list.append(k)
            else:
                missed_list.append(k)

        return matched_state, {'missed': missed_list, 'unmatched': unmatched_list}

    def _adjust_head_parameters(self, cur_state_dict, pretrain_state_dict):
        """Adjust head parameters between datasets."""
        # List of parameters to adjust
        if pretrain_state_dict['decoder.denoising_class_embed.weight'].size() != \
                cur_state_dict['decoder.denoising_class_embed.weight'].size():
            del pretrain_state_dict['decoder.denoising_class_embed.weight']

        head_param_names = [
            'decoder.enc_score_head.weight',
            'decoder.enc_score_head.bias'
        ]
        for i in range(8):
            head_param_names.append(f'decoder.dec_score_head.{i}.weight')
            head_param_names.append(f'decoder.dec_score_head.{i}.bias')

        adjusted_params = []

        for param_name in head_param_names:
            if param_name in cur_state_dict and param_name in pretrain_state_dict:
                cur_tensor = cur_state_dict[param_name]
                pretrain_tensor = pretrain_state_dict[param_name]
                adjusted_tensor = self.map_class_weights(cur_tensor, pretrain_tensor)
                if adjusted_tensor is not None:
                    pretrain_state_dict[param_name] = adjusted_tensor
                    adjusted_params.append(param_name)
                else:
                    print(f"Cannot adjust parameter '{param_name}' due to size mismatch.")

        return pretrain_state_dict

    def map_class_weights(self, cur_tensor, pretrain_tensor):
        """Map class weights from pretrain model to current model based on class IDs."""
        if pretrain_tensor.size() == cur_tensor.size():
            return pretrain_tensor

        adjusted_tensor = cur_tensor.clone()
        adjusted_tensor.requires_grad = False

        if pretrain_tensor.size() > cur_tensor.size():
            for coco_id, obj_id in enumerate(self.obj365_ids):
                adjusted_tensor[coco_id] = pretrain_tensor[obj_id+1]
        else:
            for coco_id, obj_id in enumerate(self.obj365_ids):
                adjusted_tensor[obj_id+1] = pretrain_tensor[coco_id]           

        return adjusted_tensor

    def fit(self):
        raise NotImplementedError('')

    def val(self):
        raise NotImplementedError('')

# obj365_classes = [
#         'Person', 'Sneakers', 'Chair', 'Other Shoes', 'Hat', 'Car', 'Lamp', 'Glasses', 
#         'Bottle', 'Desk', 'Cup', 'Street Lights', 'Cabinet/shelf', 'Handbag/Satchel', 
#         'Bracelet', 'Plate', 'Picture/Frame', 'Helmet', 'Book', 'Gloves', 'Storage box', 
#         'Boat', 'Leather Shoes', 'Flower', 'Bench', 'Potted Plant', 'Bowl/Basin', 'Flag', 
#         'Pillow', 'Boots', 'Vase', 'Microphone', 'Necklace', 'Ring', 'SUV', 'Wine Glass', 
#         'Belt', 'Moniter/TV', 'Backpack', 'Umbrella', 'Traffic Light', 'Speaker', 'Watch', 
#         'Tie', 'Trash bin Can', 'Slippers', 'Bicycle', 'Stool', 'Barrel/bucket', 'Van', 
#         'Couch', 'Sandals', 'Bakset', 'Drum', 'Pen/Pencil', 'Bus', 'Wild Bird', 'High Heels', 
#         'Motorcycle', 'Guitar', 'Carpet', 'Cell Phone', 'Bread', 'Camera', 'Canned', 'Truck', 
#         'Traffic cone', 'Cymbal', 'Lifesaver', 'Towel', 'Stuffed Toy', 'Candle', 'Sailboat', 
#         'Laptop', 'Awning', 'Bed', 'Faucet', 'Tent', 'Horse', 'Mirror', 'Power outlet', 
#         'Sink', 'Apple', 'Air Conditioner', 'Knife', 'Hockey Stick', 'Paddle', 'Pickup Truck', 
#         'Fork', 'Traffic Sign', 'Ballon', 'Tripod', 'Dog', 'Spoon', 'Clock', 'Pot', 'Cow', 
#         'Cake', 'Dinning Table', 'Sheep', 'Hanger', 'Blackboard/Whiteboard', 'Napkin', 
#         'Other Fish', 'Orange/Tangerine', 'Toiletry', 'Keyboard', 'Tomato', 'Lantern', 
#         'Machinery Vehicle', 'Fan', 'Green Vegetables', 'Banana', 'Baseball Glove', 
#         'Airplane', 'Mouse', 'Train', 'Pumpkin', 'Soccer', 'Skiboard', 'Luggage', 'Nightstand', 
#         'Tea pot', 'Telephone', 'Trolley', 'Head Phone', 'Sports Car', 'Stop Sign', 'Dessert', 
#         'Scooter', 'Stroller', 'Crane', 'Remote', 'Refrigerator', 'Oven', 'Lemon', 'Duck', 
#         'Baseball Bat', 'Surveillance Camera', 'Cat', 'Jug', 'Broccoli', 'Piano', 'Pizza', 
#         'Elephant', 'Skateboard', 'Surfboard', 'Gun', 'Skating and Skiing shoes', 'Gas stove', 
#         'Donut', 'Bow Tie', 'Carrot', 'Toilet', 'Kite', 'Strawberry', 'Other Balls', 'Shovel', 
#         'Pepper', 'Computer Box', 'Toilet Paper', 'Cleaning Products', 'Chopsticks', 'Microwave', 
#         'Pigeon', 'Baseball', 'Cutting/chopping Board', 'Coffee Table', 'Side Table', 'Scissors', 
#         'Marker', 'Pie', 'Ladder', 'Snowboard', 'Cookies', 'Radiator', 'Fire Hydrant', 'Basketball', 
#         'Zebra', 'Grape', 'Giraffe', 'Potato', 'Sausage', 'Tricycle', 'Violin', 'Egg', 
#         'Fire Extinguisher', 'Candy', 'Fire Truck', 'Billards', 'Converter', 'Bathtub', 
#         'Wheelchair', 'Golf Club', 'Briefcase', 'Cucumber', 'Cigar/Cigarette ', 'Paint Brush', 
#         'Pear', 'Heavy Truck', 'Hamburger', 'Extractor', 'Extention Cord', 'Tong', 
#         'Tennis Racket', 'Folder', 'American Football', 'earphone', 'Mask', 'Kettle', 
#         'Tennis', 'Ship', 'Swing', 'Coffee Machine', 'Slide', 'Carriage', 'Onion', 
#         'Green beans', 'Projector', 'Frisbee', 'Washing Machine/Drying Machine', 'Chicken', 
#         'Printer', 'Watermelon', 'Saxophone', 'Tissue', 'Toothbrush', 'Ice cream', 
#         'Hotair ballon', 'Cello', 'French Fries', 'Scale', 'Trophy', 'Cabbage', 'Hot dog', 
#         'Blender', 'Peach', 'Rice', 'Wallet/Purse', 'Volleyball', 'Deer', 'Goose', 'Tape', 
#         'Tablet', 'Cosmetics', 'Trumpet', 'Pineapple', 'Golf Ball', 'Ambulance', 'Parking meter', 
#         'Mango', 'Key', 'Hurdle', 'Fishing Rod', 'Medal', 'Flute', 'Brush', 'Penguin', 
#         'Megaphone', 'Corn', 'Lettuce', 'Garlic', 'Swan', 'Helicopter', 'Green Onion', 
#         'Sandwich', 'Nuts', 'Speed Limit Sign', 'Induction Cooker', 'Broom', 'Trombone', 
#         'Plum', 'Rickshaw', 'Goldfish', 'Kiwi fruit', 'Router/modem', 'Poker Card', 'Toaster', 
#         'Shrimp', 'Sushi', 'Cheese', 'Notepaper', 'Cherry', 'Pliers', 'CD', 'Pasta', 'Hammer', 
#         'Cue', 'Avocado', 'Hamimelon', 'Flask', 'Mushroon', 'Screwdriver', 'Soap', 'Recorder', 
#         'Bear', 'Eggplant', 'Board Eraser', 'Coconut', 'Tape Measur/ Ruler', 'Pig', 
#         'Showerhead', 'Globe', 'Chips', 'Steak', 'Crosswalk Sign', 'Stapler', 'Campel', 
#         'Formula 1 ', 'Pomegranate', 'Dishwasher', 'Crab', 'Hoverboard', 'Meat ball', 
#         'Rice Cooker', 'Tuba', 'Calculator', 'Papaya', 'Antelope', 'Parrot', 'Seal', 
#         'Buttefly', 'Dumbbell', 'Donkey', 'Lion', 'Urinal', 'Dolphin', 'Electric Drill', 
#         'Hair Dryer', 'Egg tart', 'Jellyfish', 'Treadmill', 'Lighter', 'Grapefruit', 
#         'Game board', 'Mop', 'Radish', 'Baozi', 'Target', 'French', 'Spring Rolls', 'Monkey', 
#         'Rabbit', 'Pencil Case', 'Yak', 'Red Cabbage', 'Binoculars', 'Asparagus', 'Barbell', 
#         'Scallop', 'Noddles', 'Comb', 'Dumpling', 'Oyster', 'Table Teniis paddle', 
#         'Cosmetics Brush/Eyeliner Pencil', 'Chainsaw', 'Eraser', 'Lobster', 'Durian', 'Okra', 
#         'Lipstick', 'Cosmetics Mirror', 'Curling', 'Table Tennis '
# ]

# coco_classes = ['person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus',
#                'train', 'truck', 'boat', 'traffic light', 'fire hydrant',
#                'stop sign', 'parking meter', 'bench', 'wild bird', 'cat', 'dog',
#                'horse', 'sheep', 'cow', 'elephant', 'bear', 'zebra', 'giraffe',
#                'backpack', 'umbrella', 'handbag/satchel', 'tie', 'luggage', 'frisbee',
#                'skating and skiing shoes', 'snowboard', 'baseball', 'kite', 'baseball bat',
#                'baseball glove', 'skateboard', 'surfboard', 'tennis racket',
#                'bottle', 'wine glass', 'cup', 'fork', 'knife', 'spoon', 'bowl/basin',
#                'banana', 'apple', 'sandwich', 'orange/tangerine', 'broccoli', 'carrot',
#                'hot dog', 'pizza', 'donut', 'cake', 'chair', 'couch',
#                'potted plant', 'bed', 'dinning table', 'toilet', 'moniter/tv', 'laptop',
#                'mouse', 'remote', 'keyboard', 'cell phone', 'microwave',
#                'oven', 'toaster', 'sink', 'refrigerator', 'book', 'clock',
#                'vase', 'scissors', 'stuffed toy', 'hair dryer', 'toothbrush']
