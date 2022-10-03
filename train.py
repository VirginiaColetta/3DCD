from __future__ import print_function
import numpy as np
import json
import time
import sys
from datetime import datetime
import pathlib
import shutil
import yaml
from argparse import ArgumentParser
import os
from functools import partial
from sklearn import metrics
from tqdm import tqdm, trange
import torchvision.models as models

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
from torch.utils.data import DataLoader
import torch.optim as optim

from models.SUNet18 import SUNet18
from models.ChangeFormer import ChangeFormerV6
from models.SiamUnet_conc import SiamUnet_conc
from models.Unet import Unet
from models.MTBIT import MTBIT
from models.ResNet18 import ResNet18

from dataloader import Dataset
from augmentations import get_validation_augmentations, get_training_augmentations
from losses import choose_criterion3d, choose_criterion2d
from optim import set_optimizer, set_scheduler
from cp import pretrain_strategy

def get_args():
    parser = ArgumentParser(description = "Hyperparameters", add_help = True)
    parser.add_argument('-c', '--config-name', type = str, help = 'YAML Config name', dest = 'CONFIG', default = 'config')
    parser.add_argument('-nw', '--num-workers', type = str, help = 'Number of workers', dest = 'num_workers', default = 2)
    parser.add_argument('-v', '--verbose', type = bool, help = 'Verbose validation metrics', dest = 'verbose', default = False)
    return parser.parse_args()

# to calculate rmse
def metric_mse(inputs, targets, exclude_zeros = False):
    loss = (inputs - targets) ** 2
    if exclude_zeros:
        n_pixels = np.count_nonzero(targets)
        return np.sum(loss)/n_pixels
    else:
        return np.mean(loss)

args = get_args()

device = 'cuda'
cuda = True
num_GPU = 1
torch.cuda.set_device(0)
manual_seed = 18
np.random.seed(manual_seed)
torch.manual_seed(manual_seed)

config_name = args.CONFIG
config_path = './config/'+config_name
default_dst_dir = "./results/"
out_file = default_dst_dir + config_name + '/'
os.makedirs(out_file, exist_ok=True)

# Load the configuration params of the experiment
full_config_path = config_path + ".yaml"
print(f"Loading experiment {full_config_path}")
with open(full_config_path, "r") as f:
    exp_config = yaml.load(f, Loader=yaml.SafeLoader)

print(f"Logs and/or checkpoints will be stored on {out_file}")
shutil.copyfile(full_config_path, out_file+'config.yaml')
print("Config file correctly saved!")

stats_file = open(out_file + 'stats.txt', 'a', buffering=1)
print(' '.join(sys.argv), file=stats_file)
print(' '.join(sys.argv))

print(exp_config)
print(exp_config, file=stats_file)

x_train_dir = exp_config['data']['train']['path']
x_valid_dir = exp_config['data']['val']['path']
x_test_dir = exp_config['data']['test']['path']

batch_size = exp_config['data']['train']['batch_size']

lweight2d, lweight3d = exp_config['model']['loss_weights']
weights2d = exp_config['model']['2d_loss_weights']

augmentation = exp_config['data']['augmentations']
# resize = exp_config['data']['resize_strategy']
# size = exp_config['data']['size']
# scale_factor = exp_config['data']['scale_factor']

min_scale = exp_config['data']['min_value']
max_scale = exp_config['data']['max_value']

mean = exp_config['data']['mean']
std = exp_config['data']['std']

if augmentation:
    train_transform = get_training_augmentations(m = mean, s = std)
else:
  train_transform = get_validation_augmentations(m = mean, s = std)

valid_transform = get_validation_augmentations(m = mean, s = std)

train_dataset = Dataset(x_train_dir,
                        #resize = resize, 
                        #size = size,
                        #scale_factor = scale_factor,
                        augmentation = train_transform)

valid_dataset = Dataset(x_valid_dir,
                        #resize = resize, 
                        #size = size,
                        #scale_factor = scale_factor,
                        augmentation = valid_transform)
                        
test_dataset = Dataset(x_test_dir,
                        #resize = resize, 
                        #size = size,
                        #scale_factor = scale_factor,
                        augmentation = valid_transform)


train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=args.num_workers)
valid_loader = DataLoader(valid_dataset, batch_size=1, shuffle=False, num_workers=args.num_workers)
test_loader = DataLoader(test_dataset, batch_size=1, shuffle=False, num_workers=args.num_workers)

name_3dloss = exp_config['model']['3d_loss']
exclude_zeros = exp_config['model']['exclude_zeros'] 
criterion3d = choose_criterion3d(name = name_3dloss)

class_weights2d = torch.FloatTensor(weights2d).to(device)
name_2dloss = exp_config['model']['2d_loss'] 
criterion2d = choose_criterion2d(name_2dloss, class_weights2d) #, class_ignored)

nepochs = exp_config['optim']['num_epochs']
lr = exp_config['optim']['lr']

model = exp_config['model']['model']
classes = exp_config['model']['num_classes']

pretrain = exp_config['model']['pretraining_strategy']
arch = exp_config['model']['feature_extractor_arch']
CHECKPOINTS = exp_config['model']['checkpoints_path']

encoder, pretrained, _ = pretrain_strategy(pretrain, CHECKPOINTS, arch)

if model == "SUNet18":
    net = SUNet18(3, 2, share_encoder = False, base_model = encoder).to(device)
elif model == "ResNet18":
    net = ResNet18(3, 2, share_encoder = False, base_model = encoder).to(device)
elif model == "ChangeFormer":
	net = ChangeFormerV6(3,2, decoder_softmax=False, embed_dim=32).to(device)
elif model == "SiamUnet_conc":
	net = SiamUnet_conc(3,2).to(device)
elif model == "Unet":
	net = Unet(3,2).to(device)
elif model == 'mtbit_resnet18':
  net = MTBIT(input_nc=3, output_nc=2, token_len=4, resnet_stages_num=4, if_upsample_2x=False,
              with_pos='learned', enc_depth=1, dec_depth=8, decoder_dim_head=8).to(device)
elif model == 'mtbit_resnet50':
  net = MTBIT(input_nc=3, output_nc=2, token_len=4, resnet_stages_num=4, if_upsample_2x=True,
              with_pos='learned', enc_depth=1, dec_depth=8, decoder_dim_head=16, backbone = 'resnet50').to(device)
else:
	print('Model not implemented yet')

print('Model selected: ', model)

optimizer = set_optimizer(exp_config['optim'], net)
print('Optimizer selected: ', exp_config['optim']['optim_type'])
lr_adjust = set_scheduler(exp_config['optim'], optimizer)
print('Scheduler selected: ', exp_config['optim']['lr_schedule_type'])

res_cp = exp_config['model']['restore_checkpoints']
if os.path.exists(out_file+f'{res_cp}bestnet.pth'):
  net.load_state_dict(torch.load(out_file+f'{res_cp}bestnet.pth'))
  print('Checkpoints successfully loaded!')
else:
  print('No checkpoints founded')

start = time.time()

best2dmetric = 0
best3dmetric = 1000000
  
net.train()

for epoch in range(1, nepochs):
  tot_2d_loss = 0
  tot_3d_loss = 0

  for param_group in optimizer.param_groups:
    print("Epoch: %s" % epoch, " - Learning rate: ", param_group['lr'])

  for t1, t2, mask2d, mask3d in tqdm(train_loader):

    t1 = t1.to(device)
    t2 = t2.to(device)
    
    mask3d = mask3d.to(device).float()
    mask3d = 2*(mask3d - min_scale)/(max_scale - min_scale)-1 #per Tanh    

    out2d, out3d = net(t1, t2)

    loss2d = criterion2d(out2d, mask2d.to(device).long()) #long
    loss3d = criterion3d(out3d.squeeze(dim=1), mask3d) #, exclude_zeros = exclude_zeros)

    loss = lweight2d*loss2d + lweight3d*loss3d #sommo le loss

    optimizer.zero_grad()
    loss.backward() #bacward delle loss
    optimizer.step()

    tot_2d_loss += loss2d.detach().cpu().numpy()*batch_size 
    tot_3d_loss += loss3d.detach().cpu().numpy()*batch_size

  epoch_2d_loss = tot_2d_loss/len(train_dataset)
  epoch_3d_loss = tot_3d_loss/len(train_dataset)
  epoch_loss = lweight2d*epoch_2d_loss + lweight3d*epoch_3d_loss
  
  lr_adjust.step()
  
  print(f"Training loss: {epoch_loss},\t2D Loss: {epoch_2d_loss}, \t3D Loss: {epoch_3d_loss}")

  with torch.no_grad():
    net.eval()

    TN = 0
    FP = 0
    FN = 0
    TP = 0
    mean_mae = 0
    rmse1 = 0
    rmse2 = 0

    for t1, t2, mask2d, mask3d in tqdm(valid_loader): 

      t1 = t1.to(device)
      t2 = t2.to(device)

      out2d, out3d = net(t1, t2)
      out2d = out2d.detach().argmax(dim=1).cpu().numpy()
      out3d = out3d.detach().cpu().numpy()
      out3d = ((out3d.ravel()+1)/2)*(max_scale-min_scale)+min_scale
     
      try:
          tn, fp, fn, tp = metrics.confusion_matrix(mask2d.ravel(), out2d.ravel()).ravel()
      except: 
          tn, fp, fn, tp = [0,0,0,0]
          print('Only 0 mask') 
	
      mean_ae = metrics.mean_absolute_error(mask3d.ravel(), out3d.ravel())
      s_rmse1 = metric_mse(out3d.ravel(), mask3d.cpu().numpy().ravel(), exclude_zeros = False)
      s_rmse2 = metric_mse(out3d.ravel(), mask3d.cpu().numpy().ravel(), exclude_zeros = True)
      max_error = metrics.max_error(mask3d.ravel(), out3d.ravel())
      mask_max = np.abs(mask3d.cpu().numpy()).max()    

      TN += tn
      FP += fp
      FN += fn 
      TP += tp
      mean_mae += mean_ae
      rmse1 += s_rmse1
      rmse2 += s_rmse2
    
    F1 = 2*TP/(2*TP+FN+FP)
    IoU = TP/(TP+FN+FP)
    mean_mae = mean_mae/len(valid_loader)
    RMSE1 = np.sqrt(rmse1/len(valid_loader))
    RMSE2 = np.sqrt(rmse2/len(valid_loader))
    
    print(f'Validation metrics - 2D: F1 Score -> {F1*100} %; mIoU -> {IoU*100} %; 3D: MAE -> {mean_mae} m; RMSE -> {RMSE1} m; cRMSE -> {RMSE2} m')

    if F1 > best2dmetric:
      best2dmetric = F1
      torch.save(net.state_dict(), out_file+'/2dbestnet.pth')
      print('Best 2D model saved!')

    if RMSE2 < best3dmetric:
      best3dmetric = RMSE2
      torch.save(net.state_dict(), out_file+'/3dbestnet.pth')  
      print('Best 3D model saved!')

  stats = dict(epoch = epoch, Loss2D = epoch_2d_loss, Loss3D = epoch_3d_loss, Loss = epoch_loss, RMSE = RMSE1, cRMSE = RMSE2, F1Score = F1*100, IoU = IoU*100)
  print(json.dumps(stats), file=stats_file)

end = time.time()
print('Training completed. Program processed ', end - start, 's, ', (end - start)/60, 'min, ', (end - start)/3600, 'h')
print(f'Best metrics: F1 score -> {best2dmetric*100} %,\t cRMSE -> {best3dmetric}')

start = time.time()

if os.path.exists('%s/' % out_file + f'{res_cp}bestnet.pth'):
    net.load_state_dict(torch.load('%s/' % out_file + f'{res_cp}bestnet.pth'))
    print("Checkpoints correctly loaded: ", out_file)

net.eval()

TN = 0
FP = 0
FN = 0
TP = 0
mean_mae = 0
rmse1 = 0
rmse2 = 0

for t1, t2, mask2d, mask3d in tqdm(test_loader): 

  t1 = t1.to(device)
  t2 = t2.to(device)

  out2d, out3d = net(t1, t2)
  out2d = out2d.detach().argmax(dim=1)
  out2d = out2d.cpu().numpy()
  out3d = out3d.detach().cpu().numpy()
  out3d = (out3d + 1)*(max_scale - min_scale)/2 + min_scale #Tanh  

  try:
      tn, fp, fn, tp = metrics.confusion_matrix(mask2d.ravel(), out2d.ravel()).ravel()
  except: 
      tn, fp, fn, tp = [0,0,0,0]
      print('Only 0 mask') 

  mean_ae = metrics.mean_absolute_error(mask3d.ravel(), out3d.ravel())
  s_rmse1 = metric_mse(out3d.ravel(), mask3d.cpu().numpy().ravel(), exclude_zeros = False)
  s_rmse2 = metric_mse(out3d.ravel(), mask3d.cpu().numpy().ravel(), exclude_zeros = True)
  max_error = metrics.max_error(mask3d.ravel(), out3d.ravel())
  mask_max = np.abs(mask3d.cpu().numpy()).max()
  
  if args.verbose:
    print()
    print(f'2D Val: TN: {tn},\tFN: {fn},\tTP: {tp},\tFP: {fp}')
    print(f'3D Val: Mean Absolute Error: {mean_ae}, \tRMSE Error: {s_rmse}, \tMax Error: {max_error} (w.r.t {mask_max})')

  TN += tn
  FP += fp
  FN += fn 
  TP += tp
  mean_mae += mean_ae
  rmse1 += s_rmse1
  rmse2 += s_rmse2
	
F1 = 2*TP/(2*TP+FN+FP)
IoU = TP/(TP+FN+FP)
mean_mae = mean_mae/len(test_loader)
RMSE1 = np.sqrt(rmse1/len(test_loader))
RMSE2 = np.sqrt(rmse2/len(test_loader))

end = time.time()
print('Test completed. Program processed ', end - start, 's, ', (end - start)/60, 'min, ', (end - start)/3600, 'h')
print(f'Test metrics - 2D: F1 Score -> {F1*100} %; mIoU -> {IoU*100} %; 3D: MAE -> {mean_mae} m; RMSE -> {RMSE1} m; cRMSE -> {RMSE2} m')
stats = dict(epoch = 'Test', MeanAbsoluteError = mean_mae, RMSE = RMSE1, cRMSE = RMSE2, F1Score = F1*100, IoU = IoU*100)
print(json.dumps(stats), file=stats_file)
