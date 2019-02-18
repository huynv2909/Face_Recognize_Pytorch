#!/usr/bin/env python
# encoding: utf-8
'''
@author: wujiyang
@contact: wujiyang@hust.edu.cn
@file: train.py.py
@time: 2018/12/21 17:37
@desc: train script for deep face recognition
'''

import os
import torch.utils.data
from torch.nn import DataParallel
from datetime import datetime
from backbone.model import SE_IR, MobileFaceNet, l2_norm
from backbone.model_irse import IR_50
from margin.ArcMarginProduct import ArcMarginProduct
from utils.visualize import Visualizer
from utils.logging import init_log
from dataset.casia_webface import CASIAWebFace
from dataset.VGG_FP import VGG_FP
from config import get_config_train
from dataset.lfw import LFW
from dataset.agedb import AgeDB30
from dataset.cfp import CFP_FP
from torch.optim import lr_scheduler
import torch.optim as optim
import time
from eval.eval_lfw import evaluation_10_fold, getFeatureFromTorch
import numpy as np
import torchvision.transforms as transforms
import argparse
from torchsummary import summary
config = get_config_train()
def train(args):
    # gpu init
    multi_gpus = False
    if len(args.gpus.split(',')) > 1:
        multi_gpus = True
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpus
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # log init
    save_dir = os.path.join(args.save_dir, args.backbone.upper() + datetime.now().strftime('%Y%m%d_%H%M%S'))
    if os.path.exists(save_dir):
        raise NameError('model dir exists!')
    os.makedirs(save_dir)
    logging = init_log(save_dir)
    _print = logging.info

    # dataset loader
    transform = transforms.Compose([
        transforms.Resize((112, 112)),
        transforms.ToTensor(),  # range [0, 255] -> [0.0,1.0]
        transforms.Normalize(mean=(0.5, 0.5, 0.5), std=(0.5, 0.5, 0.5))  # range [0.0, 1.0] -> [-1.0,1.0]
    ])
    # validation dataset
    trainset = VGG_FP(transform=transform)
    trainloader = torch.utils.data.DataLoader(trainset, batch_size = config.batch_size,
                                             shuffle=True, num_workers=4, drop_last=False)
    num_iter = trainset.num_iter

    numclass = trainset.class_nums

    lfwdataset = LFW(transform=transform)
    lfwloader = torch.utils.data.DataLoader(lfwdataset, batch_size=config.batch_size,
                                             shuffle=False, num_workers=4, drop_last=False)
    agedbdataset = AgeDB30(transform=transform)
    agedbloader = torch.utils.data.DataLoader(agedbdataset, batch_size=config.batch_size,
                                            shuffle=False, num_workers=4, drop_last=False)
    cfpfpdataset = CFP_FP(transform=transform)
    cfpfploader = torch.utils.data.DataLoader(cfpfpdataset, batch_size=config.batch_size,
                                              shuffle=False, num_workers=4, drop_last=False)

    # define backbone and margin layer
    if args.backbone == 'MobileFace':
        net = MobileFaceNet(512).to(config.device)
    elif args.backbone == 'SERes50_IR':
        net = SE_IR(50, 0.6, 'ir_se').to(config.device)
    elif args.backbone == 'IR_50':
        net = IR_50((112, 112)).to(config.device)
    else:
        print(args.backbone, ' is not available!')

    if args.margin_type == 'ArcFace':
        margin = ArcMarginProduct(512, numclass, s=args.scale_size)
    elif args.margin_type == 'CosFace':
        pass
    elif args.margin_type == 'SphereFace':
        pass
    else:
        print(args.margin_type, 'is not available!')
    if args.resume:
        print('resume the model parameters from: ', args.net_path, args.margin_path)
        net.load_state_dict(torch.load(args.net_path))
        #margin.load_state_dict(torch.load(args.margin_path)['net_state_dict'])

    # define optimizers for different layer
    criterion = torch.nn.CrossEntropyLoss().to(device)
    optimizer_ft = optim.SGD([
        {'params': net.parameters(), 'weight_decay': 4e-4},
        {'params': margin.parameters(), 'weight_decay': 4e-4}
    ], lr=0.010, momentum=0.9, nesterov=True)
    exp_lr_scheduler = lr_scheduler.MultiStepLR(optimizer_ft, milestones= config.milestones, gamma=0.1)

    if multi_gpus:
        net = DataParallel(net).to(device)
        margin = DataParallel(margin).to(device)
    else:
        net = net.to(device)
        margin = margin.to(device)
    best_lfw_acc = 0.0
    best_lfw_iters = 0
    best_agedb30_acc = 0.0
    best_agedb30_iters = 0
    best_cfp_fp_acc = 0.0
    best_cfp_fp_iters = 0
    total_iters = 0
    vis = Visualizer(env= args.backbone)
    start_epoch = total_iters//num_iter
    print(start_epoch)
    for epoch in range(1, args.total_epoch + 1):
        exp_lr_scheduler.step()
        if epoch < start_epoch:
            continue
        # train model
        _print('Train Epoch: {}/{} ...'.format(epoch, args.total_epoch))
        net.train()
        log_vis_train = open('result/log_vis_train.txt', 'a')
        log_vis_test = open('result/log_vis_test.txt', 'a')

        since = time.time()
        for data in trainloader:
            img, label = data[0].to(device), data[1].to(device)
            optimizer_ft.zero_grad()

            raw_logits = net(img)
            output = margin(raw_logits, label)
            total_loss = criterion(output, label)
            total_loss.backward()
            optimizer_ft.step()

            
            # print train information
            if total_iters % 200 == 0:
                # current training accuracy
                _, predict = torch.max(output.data, 1)
                total = label.size(0)
                correct = (np.array(predict) == np.array(label.data)).sum()
                time_cur = (time.time() - since) / 100
                since = time.time()
                vis.plot_curves({'softmax loss': total_loss.item()}, iters=total_iters, title='train loss',
                                xlabel='iters', ylabel='train loss')
                vis.plot_curves({'train accuracy': correct / total}, iters=total_iters, title='train accuracy', xlabel='iters',
                                ylabel='train accuracy')
                log_vis_train.write("%d:%f:%f\n"%(total_iters,total_loss.item(), (correct / total)))

                print("Iters: {:0>6d}/[{:0>2d}], loss: {:.4f}, train_accuracy: {:.4f}, time: {:.2f} s/iter, learning rate: {}".format(total_iters, epoch, total_loss.item(), correct/total, time_cur, exp_lr_scheduler.get_lr()[0]))

            # save model
            if total_iters % args.save_freq == 0:
                msg = 'Saving checkpoint: {}'.format(total_iters)
                _print(msg)
                if multi_gpus:
                    net_state_dict = net.module.state_dict()
                    margin_state_dict = margin.module.state_dict()
                else:
                    net_state_dict = net.state_dict()
                    margin_state_dict = margin.state_dict()
                if not os.path.exists(save_dir):
                    os.mkdir(save_dir)
                torch.save({
                    'iters': total_iters,
                    'net_state_dict': net_state_dict},
                    os.path.join(save_dir, 'Iter_%06d_net.ckpt' % total_iters))
                torch.save({
                    'iters': total_iters,
                    'net_state_dict': margin_state_dict},
                    os.path.join(save_dir, 'Iter_%06d_margin.ckpt' % total_iters))

            # test accuracy
            if total_iters % args.test_freq == 0:

                # test model on lfw
                net.eval()
                getFeatureFromTorch('./result/cur_lfw_result.mat', net, device, lfwdataset, lfwloader)
                lfw_accs = evaluation_10_fold('./result/cur_lfw_result.mat')
                _print('LFW Ave Accuracy: {:.4f}'.format(np.mean(lfw_accs) * 100))
                if best_lfw_acc <= np.mean(lfw_accs) * 100:
                    best_lfw_acc = np.mean(lfw_accs) * 100
                    best_lfw_iters = total_iters

                # test model on AgeDB30
                getFeatureFromTorch('./result/cur_agedb30_result.mat', net, device, agedbdataset, agedbloader)
                age_accs = evaluation_10_fold('./result/cur_agedb30_result.mat')
                _print('AgeDB-30 Ave Accuracy: {:.4f}'.format(np.mean(age_accs) * 100))
                if best_agedb30_acc <= np.mean(age_accs) * 100:
                    best_agedb30_acc = np.mean(age_accs) * 100
                    best_agedb30_iters = total_iters

                # test model on CFP-FP
                getFeatureFromTorch('./result/cur_cfpfp_result.mat', net, device, cfpfpdataset, cfpfploader)
                cfp_accs = evaluation_10_fold('./result/cur_cfpfp_result.mat')
                _print('CFP-FP Ave Accuracy: {:.4f}'.format(np.mean(cfp_accs) * 100))
                if best_cfp_fp_acc <= np.mean(cfp_accs) * 100:
                    best_cfp_fp_acc = np.mean(cfp_accs) * 100
                    best_cfp_fp_iters = total_iters
                _print('Current Best Accuracy: LFW: {:.4f} in iters: {}, AgeDB-30: {:.4f} in iters: {} and CFP-FP: {:.4f} in iters: {}'.format(
                    best_lfw_acc, best_lfw_iters, best_agedb30_acc, best_agedb30_iters, best_cfp_fp_acc, best_cfp_fp_iters))
                _print('Current Best Accuracy:LFW: {:.4f} in iters: {} and CFP-FP: {:.4f} in iters: {}'.format(
                                            best_lfw_acc, best_lfw_iters, best_cfp_fp_acc, best_cfp_fp_iters))

                vis.plot_curves({'lfw': np.mean(lfw_accs), 'agedb-30': np.mean(age_accs), 'cfp-fp': np.mean(cfp_accs)}, iters=total_iters,
                                title='test accuracy', xlabel='iters', ylabel='test accuracy')
                vis.plot_curves({'lfw': np.mean(lfw_accs), 'cfp-fp': np.mean(cfp_accs)}, iters=total_iters,
                                title='test accuracy', xlabel='iters', ylabel='test accuracy')
                vis.plot_curves({'cfp-fp': np.mean(cfp_accs)}, iters=total_iters,
                                title='test accuracy', xlabel='iters', ylabel='test accuracy')
                log_vis_test.write('%d:%f:%f\n'%(total_iters, np.mean(lfw_accs), np.mean(cfp_accs)))
                net.train()
            total_iters += 1

    _print('Finally Best Accuracy: LFW: {:.4f} in iters: {}, AgeDB-30: {:.4f} in iters: {} and CFP-FP: {:.4f} in iters: {}'.format(
        best_lfw_acc, best_lfw_iters, best_agedb30_acc, best_agedb30_iters, best_cfp_fp_acc, best_cfp_fp_iters))
    _print('Finally Best Accuracy: LFW: {:.4f} in iters: {} and CFP-FP: {:.4f} in iters: {}'.format(
                                            best_lfw_acc, best_lfw_iters, best_cfp_fp_acc, best_cfp_fp_iters))
    print('finishing training')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PyTorch for deep face recognition')
    parser.add_argument('--train_root', type=str, default='/media/ramdisk/msra_align_112', help='train image root')
    parser.add_argument('--train_file_list', type=str, default='/media/ramdisk/msra_align_train.list', help='train list')
    parser.add_argument('--lfw_test_root', type=str, default='/media/ramdisk/lfw_align_112', help='lfw image root')
    parser.add_argument('--lfw_file_list', type=str, default='/media/ramdisk/pairs.txt', help='lfw pair file list')
    parser.add_argument('--agedb_test_root', type=str, default='/media/sda/AgeDB-30/agedb30_align_112', help='agedb image root')
    parser.add_argument('--agedb_file_list', type=str, default='/media/sda/AgeDB-30/agedb_30_pair.txt', help='agedb pair file list')
    parser.add_argument('--cfpfp_test_root', type=str, default='/media/sda/CFP-FP/cfp_fp_aligned_112', help='agedb image root')
    parser.add_argument('--cfpfp_file_list', type=str, default='/media/sda/CFP-FP/cfp_fp_pair.txt', help='agedb pair file list')

    parser.add_argument('--backbone', type=str, default='CBAMRes50_IR', help='MobileFace, Res50_IR, SERes50_IR, SphereNet, SERes100_IR, CBAMRes50_IR, CBAMRes50_AIR')
    parser.add_argument('--margin_type', type=str, default='ArcFace', help='ArcFace, CosFace, SphereFace')
    parser.add_argument('--feature_dim', type=int, default=512, help='feature dimension, 128 or 512')
    parser.add_argument('--scale_size', type=float, default=32.0, help='scale size')
    parser.add_argument('--batch_size', type=int, default=256, help='batch size')
    parser.add_argument('--total_epoch', type=int, default=300, help='total epochs')

    parser.add_argument('--save_freq', type=int, default=9000, help='save frequency')
    parser.add_argument('--test_freq', type=int, default=9000, help='test frequency')
    parser.add_argument('--resume', type=int, default=1, help='resume model')
    parser.add_argument('--net_path', type=str, default='model/MSCeleb_SERES50_IR/model_ir_se50.pth', help='resume model')
    parser.add_argument('--margin_path', type=str, default='model/MSCeleb_SERES50_IR/Iter_192000_margin.ckpt', help='resume model')
    parser.add_argument('--save_dir', type=str, default='./weights', help='model save dir')
    parser.add_argument('--gpus', type=str, default='0', help='model prefix')

    args = parser.parse_args()

    train(args)

