from .base_model import BaseModel
from wavenet_models import STMF_network
import torch
from torch.autograd import Variable
import os
from collections import OrderedDict

from util import util

class STMFModel(BaseModel):
    def name(self):
        return "RRDBModel"

    def initialize(self, opt):
        BaseModel.initialize(self, opt)
        self.opt = opt

        self.save_dir=os.path.join(opt.checkpoints_dir,opt.name)
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)
        if len(self.opt.gpu_ids)>0:
            self.state = Variable(torch.zeros(self.opt.batch_size, 256, int(self.opt.image_size_x/8), int(self.opt.image_size_y/8)).cuda(), requires_grad = False)
        else:
            self.state = Variable(torch.zeros(self.opt.batch_size, 256, int(self.opt.image_size_x/8), int(self.opt.image_size_y/8)), requires_grad = False)


        self.inputs = []
        for i in range(self.opt.K + self.opt.T):
            self.inputs.append(self.Tensor(self.opt.batch_size, self.opt.c_dim, self.opt.image_size_x, self.opt.image_size_y))

        self.generator = STMF_network.define_generator(opt)

        self.updateD = True
        self.updateG = True

        if self.opt.is_train:
            self.loss_Lp = torch.nn.MSELoss()

            self.loss_gdl = STMF_network.define_gdl(opt.c_dim, self.gpu_ids)

            # initialize optimizers
            self.schedulers = []
            self.optimizers = []
            self.optimizer_G = torch.optim.Adam(self.generator.parameters(),
                                                lr=opt.lr, betas=(opt.beta1, 0.999))

            if opt.adversarial:
                self.discriminator = STMF_network.define_discriminator([opt.image_size_x,opt.image_size_y], opt.c_dim, self.opt.K, self.opt.T,
                                                                          opt.df_dim, gpu_ids=self.gpu_ids)
                self.loss_d = torch.nn.BCELoss()
                self.optimizer_D = torch.optim.Adam(self.discriminator.parameters(),
                                                    lr=opt.lr, betas=(opt.beta1, 0.999))

            if not self.opt.is_train or self.opt.continue_train:
                self.load(self.opt.which_epoch)

            if self.opt.is_train:
                self.optimizers.append(self.optimizer_G)
                if self.opt.adversarial:
                    self.optimizers.append(self.optimizer_D)

            for optimizer in self.optimizers:
                self.schedulers.append(STMF_network.get_scheduler(optimizer, opt))


    def forward(self):
        #input:(K+T)*BCHW
        self.pred = self.generator.forward(self.inputs, self.state)#pred: T*BCHW
    


    def validate(self,inputs,keep_state=True):
        if keep_state:
            old_state=self.state
        self.set_inputs(inputs)
        self.forward()
        the_pred=self.pred
        targets=inputs[:,:,:,:,-self.opt.T:]
        count=0
        pr=0
        for i in range(len(the_pred)):
            target_batch=targets[:,:,:,:,i]
            pred_batch=the_pred[i]
            for j in range(targets.shape[0]):
                #print(target_batch[j])
                #print(pred_batch[j])
                pr+=util.psnr(target_batch[j].cpu(),pred_batch[j].cpu())
                count+=1
        if keep_state:
            self.state=old_state
        return pr/count


        


    def backward_D(self):
        # fake
        input_fake = torch.cat(self.inputs[:self.opt.K] + self.pred, dim=1)
        input_fake_ = Variable(input_fake.data)
        h_sigmoid, h = self.discriminator.forward(input_fake_, self.opt.batch_size)
        if len(self.gpu_ids) > 0:
            labels = Variable(torch.zeros(h.size()).cuda())
        else:
            labels = Variable(torch.zeros(h.size()))
        self.loss_d_fake = self.loss_d(h_sigmoid, labels)

        # real
        input_real = torch.cat(self.inputs, dim=1)
        input_real_ = Variable(input_real.data)
        h_sigmoid_, h_ = self.discriminator.forward(input_real_, self.opt.batch_size)
        if len(self.gpu_ids) > 0:
            labels_ = Variable(torch.ones(h_.size()).cuda())
        else:
            labels_ = Variable(torch.ones(h_.size()))
        self.loss_d_real = self.loss_d(h_sigmoid_, labels_)

        self.loss_D = self.loss_d_fake + self.loss_d_real

        self.loss_D.backward()

    def backward_G(self):
        outputs = util.inverse_transform(torch.cat(self.pred, dim=0))
        targets = util.inverse_transform(torch.cat(self.inputs[self.opt.K:], dim=0))
        self.Lp = self.loss_Lp(outputs, targets)
        # pdb.set_trace()
        self.gdl = self.loss_gdl(outputs, targets)
        self.loss_G = self.opt.alpha * (self.Lp + self.gdl)

        if self.opt.adversarial:
            input_fake = torch.cat(self.inputs[:self.opt.K] + self.pred, dim=1)
            h_sigmoid, h = self.discriminator.forward(input_fake, self.opt.batch_size)
            if len(self.gpu_ids) > 0:
                labels = Variable(torch.ones(h.size()).cuda())
            else:
                labels = Variable(torch.ones(h.size()))
            self.L_GAN = self.loss_d(h_sigmoid, labels)

            if not self.updateD:
                if len(self.gpu_ids) > 0:
                    labels_ = Variable(torch.zeros(h.size()).cuda())
                else:
                    labels_ = Variable(torch.zeros(h.size()))
                self.loss_d_fake = self.loss_d(h_sigmoid, labels_)

                input_real = torch.cat(self.inputs, dim=1)
                input_real_ = Variable(input_real.data)
                h_sigmoid_, h_ = self.discriminator.forward(input_real_, self.opt.batch_size)
                # print('in real, h:', h_)
                if len(self.gpu_ids) > 0:
                    labels__ = Variable(torch.ones(h_.size()).cuda())
                else:
                    labels__ = Variable(torch.ones(h_.size()))
                self.loss_d_real = self.loss_d(h_sigmoid_, labels__)
            self.loss_G += self.opt.beta * self.L_GAN

        self.loss_G.backward()



    def optimize_parameters(self):

        self.forward()
        if not self.opt.adversarial:
            self.optimizer_G.zero_grad()
            self.backward_G()
            self.optimizer_G.step()
        else:
            if self.opt.D_G_switch == 'adaptive':#unfinished
                if self.updateD:
                    self.optimizer_D.zero_grad()
                    self.backward_D()
                    self.optimizer_D.step()

                if self.updateG:
                    self.optimizer_G.zero_grad()
                    self.backward_G()
                    self.optimizer_G.step()

                if self.loss_d_fake.item() < self.opt.margin or self.loss_d_real.item() < self.opt.margin:
                    self.updateD = False

                if self.loss_d_fake.item() > (1. - self.opt.margin) or self.loss_d_real.item() > (1.- self.opt.margin):
                    self.updateG = False

                if not self.updateD and not self.updateG:
                    self.updateD = True
                    self.updateG = True

            elif self.opt.D_G_switch == 'alternative':
                self.optimizer_D.zero_grad()
                self.backward_D()
                self.optimizer_D.step()

                self.optimizer_G.zero_grad()
                self.backward_G()
                self.optimizer_G.step()
            else:
                raise NotImplementedError('switch method [%s] is not implemented' % self.opt.D_G_switch)




    def get_current_errors(self):
        if not self.opt.no_adversarial:
            return OrderedDict([('G_GAN', self.L_GAN.item()),
                                ('G_Lp', self.Lp.item()),
                                ('G_gdl', self.gdl.item()),
                                ('G_loss', self.loss_G.item()),
                                ('D_real', self.loss_d_real.item()),
                                ('D_fake', self.loss_d_fake.item())
                                ])
        else:
            return OrderedDict([('G_Lp', self.Lp.item()),
                                ('G_gdl', self.gdl.item()),
                                ('G_loss', self.loss_G.item())
                                ])
    def set_inputs(self, input):

        self.data = input
        self.inputs = []
        f_volatile = not self.updateG or not self.is_train

        if len(self.gpu_ids) > 0:
            for i in range(self.opt.K + self.opt.T):
                self.inputs.append(Variable(input[:, :, :, :, i].cuda(), volatile=f_volatile))
        else:
            for i in range(self.opt.K + self.opt.T):
                self.inputs.append(Variable(input[:, :, :, :, i], volatile=f_volatile))



    def save(self, label, epoch):
        current_state = {
            "epoch": epoch,
            "generator": self.generator.cpu().state_dict(),
            "discriminator": self.discriminator.cpu().state_dict() if self.opt.adversarial else None,
            "optimizer_G": self.optimizer_G.state_dict(),
            "optimizer_D": self.optimizer_D.state_dict() if self.opt.adversarial else None,
            "updateD": self.updateD if self.opt.adversarial else None,
            "updateG": self.updateG,
        }
        save_filename = '%s_model.pth.tar' % (label)
        save_path = os.path.join(self.save_dir, save_filename)
        torch.save(current_state, save_path)
        if len(self.gpu_ids) and torch.cuda.is_available():
            self.generator.cuda(device=self.gpu_ids[0])
            self.discriminator.cuda(device=self.gpu_ids[0])


    def load(self, epoch_label):
        save_filename = '%s_model.pth.tar' % (epoch_label)
        save_path = os.path.join(self.save_dir, save_filename)
        if os.path.isfile(save_path):
            print("=> loading snapshot from {}".format(save_path))
            snapshot = torch.load(save_path)
            self.start_epoch = snapshot['epoch'] + 1
            self.generator.load_state_dict(snapshot['generator'])
            if self.is_train:
                self.optimizer_G.load_state_dict(snapshot["optimizer_G"])
                if self.opt.adversarial:
                    self.discriminator.load_state_dict(snapshot['discriminator'])
                    self.optimizer_D.load_state_dict(snapshot["optimizer_D"])
            self.updateD = snapshot['updateD']
            self.updateG = snapshot['updateG']


    def get_current_errors(self):
        if self.opt.adversarial:
            return OrderedDict([('G_GAN', self.L_GAN.item()),
                                ('G_Lp', self.Lp.item()),
                                ('G_gdl', self.gdl.item()),
                                ('G_loss', self.loss_G.item()),
                                ('D_real', self.loss_d_real.item()),
                                ('D_fake', self.loss_d_fake.item())
                                ])
        else:
            return OrderedDict([('G_Lp', self.Lp.item()),
                                ('G_gdl', self.gdl.item()),
                                ('G_loss', self.loss_G.item())
                                ])
