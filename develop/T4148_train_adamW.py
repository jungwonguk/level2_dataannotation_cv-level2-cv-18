import sys
import os
import errno
import os.path as osp
import time
import math
from datetime import datetime, timedelta
from argparse import ArgumentParser

import torch
from torch import cuda
from torch.utils.data import DataLoader
from torch.optim import lr_scheduler
from tqdm import tqdm

import numpy as np
import random

from east_dataset import EASTDataset
from dataset import SceneTextDataset
from model import EAST

import wandb


def symlink_force(target, link_name):
    try:
        os.symlink(target, link_name)
    except OSError as e:
        if e.errno == errno.EEXIST:
            os.remove(link_name)
            os.symlink(target, link_name)
        else:
            raise e


def parse_args():
    parser = ArgumentParser()

    # Conventional args
    parser.add_argument('--data_dir', type=str,
                        default=os.environ.get('SM_CHANNEL_TRAIN', '../input/data/ICDAR17_Korean'))
    parser.add_argument('--model_dir', type=str, default=os.environ.get('SM_MODEL_DIR',
                                                                        'trained_models'))

    parser.add_argument('--device', default='cuda' if cuda.is_available() else 'cpu')
    parser.add_argument('--num_workers', type=int, default=4)

    parser.add_argument('--image_size', type=int, default=1024)
    parser.add_argument('--input_size', type=int, default=512)
    parser.add_argument('--batch_size', type=int, default=12)
    parser.add_argument('--learning_rate', type=float, default=1e-3)
    parser.add_argument('--max_epoch', type=int, default=200)
    parser.add_argument('--save_interval', type=int, default=5)
    parser.add_argument('--wandb_name', type=str, default='Unnamed Test')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--early_stop', type=int, default=201)

    args = parser.parse_args()

    if args.input_size % 32 != 0:
        raise ValueError('`input_size` must be a multiple of 32')

    return args


def do_training(data_dir, model_dir, device, image_size, input_size, num_workers, batch_size,
                learning_rate, max_epoch, save_interval, wandb_name, seed, early_stop):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed) # if use multi-GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)

    dataset = SceneTextDataset(data_dir, split='train', image_size=image_size, crop_size=input_size)
    dataset = EASTDataset(dataset)
    num_batches = math.ceil(len(dataset) / batch_size)
    train_loader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=num_workers, worker_init_fn=np.random.seed(seed))

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model = EAST()
    model.to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, betas=(0.9,0.999), weight_decay=0.01)
    scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=[max_epoch // 2], gamma=0.1)

    early_stop_cnt=0
    best_score = 9999 #현재는 epoch_loss기준이라 이렇게 설정
    model.train()
    for epoch in range(max_epoch):
        epoch_loss, epoch_start = 0, time.time()
        with tqdm(total=num_batches) as pbar:
            for img, gt_score_map, gt_geo_map, roi_mask in train_loader:
                pbar.set_description('[Epoch {}]'.format(epoch + 1))

                loss, extra_info = model.train_step(img, gt_score_map, gt_geo_map, roi_mask)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                loss_val = loss.item()
                epoch_loss += loss_val

                pbar.update(1)
                val_dict = {
                    'Cls loss': extra_info['cls_loss'], 'Angle loss': extra_info['angle_loss'],
                    'IoU loss': extra_info['iou_loss']
                }
                pbar.set_postfix(val_dict)
                wandb.log(val_dict)

        scheduler.step()

        
        print('Mean loss: {:.4f} | Elapsed time: {} | early stop count : {}'.format(
            epoch_loss / num_batches, timedelta(seconds=time.time() - epoch_start), early_stop_cnt))

        if best_score > epoch_loss : # 이후에 요 epoch_loss와 부등호만 반대로 해주면 f1성능으로 평가 가능
            best_score = epoch_loss
            print(f'New Best Model ->Epoch [{epoch+1}] / best_score : [{best_score}]')
            best_pth_name = f'best_model.pth'
            ckpt_fpath = osp.join(model_dir, best_pth_name)
            torch.save(model.state_dict(),ckpt_fpath)
            #symlink_force(pth_name, osp.join(model_dir, "latest.pth"))
            #원하는 경우 best로 설정


        else:
            early_stop_cnt +=1

        if early_stop_cnt > early_stop:
            print(f'no more best model training')
            break
            

        if (epoch + 1) % save_interval == 0:
            if not osp.exists(model_dir):
                os.makedirs(model_dir)

            now = datetime.now()
            pth_name = f'{epoch+1}epoch_{now.strftime("%y%m%d_%H%M%S")}.pth'

            ckpt_fpath = osp.join(model_dir, pth_name)
            torch.save(model.state_dict(), ckpt_fpath)
            symlink_force(pth_name, osp.join(model_dir, "latest.pth"))
        
        


def main(args):
    wandb.init(project="OCR Data annotation",
               entity="light-observer",
               name=args.wandb_name
              )
    do_training(**args.__dict__)


if __name__ == '__main__':
    args = parse_args()
    main(args)
