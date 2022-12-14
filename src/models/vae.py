import math
import numpy as np

import torch
from torch import Tensor
from torch import nn
from torch.autograd import Variable
import time

# from models.ode_funcs import NeuralODE, ODEFunc
from models.ode_funcs import ODEFunc, NeuralODE
from models.spirals import NNODEF
from helpers.utils import reparameterize, call_gru_d
from models.GRU_D import GRUD_cell

np.set_printoptions(threshold=500)

class RNNEncoder(nn.Module):
    def __init__(self, input_dim, hidden_dim, latent_dim, encoder='gru'):
        super(RNNEncoder, self).__init__()
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.latent_dim = latent_dim
        self.encoder = encoder
        self.allowed_encoders = ['gru', 'grud']
        if encoder == 'gru':
            self.rnn = nn.GRU(input_dim + 1, hidden_dim)
        elif encoder == 'grud':
            self.rnn = GRUD_cell(input_dim, hidden_dim)
        else:
            raise ValueError("Invalid encoder! Encoder specified: {}, Allowed Encoders: {}".format(encoder, self.allowed_encoders))

        self.hid2lat = nn.Linear(hidden_dim, 2 * latent_dim)

    def forward(self, x, t):
        t = t.clone()
        # 1st element is t = 0, remainder are negative offset from that
        t[1:] = t[:-1] - t[1:]
        t[0] = 0.

        # concatenate input
        xt = torch.cat((x, t), dim=-1)

        # sample from initial encoding
        if self.encoder == 'gru':
             _, h0 = self.rnn(xt.flip((0,)).float())
        elif self.encoder == 'grud':
            _, h0 = call_gru_d(self.rnn, xt.flip((0,)).float())
            h0 = torch.permute(torch.unsqueeze(h0[:, -1, :], 1), (1, 0, 2))
        else:
            raise ValueError("Invalid encoder! Encoder specified: {}, Allowed Encoders: {}".format(self.encoder, self.allowed_encoders))

        z0 = self.hid2lat(h0[0])
        mu = z0[:, :self.latent_dim]
        logvar = z0[:, self.latent_dim:]
        return mu, logvar

class NeuralODEDecoder(nn.Module):
    def __init__(self, output_dim, hidden_dim, latent_dim):
        super(NeuralODEDecoder, self).__init__()
        self.output_dim = output_dim
        self.hidden_dim = hidden_dim
        self.latent_dim = latent_dim

        # func = NNODEF(latent_dim, hidden_dim, time_invariant=True)
        func = ODEFunc(latent_dim, hidden_dim, time_invariant=True)
        self.ode = NeuralODE(func)
        self.l2h = nn.Linear(latent_dim, hidden_dim)
        self.h2o = nn.Linear(hidden_dim, output_dim)

    def forward(self, z0, t):
        """"
            z0:
            t: number of timesteps
        """
        t_1d = t[:, 0, 0]

        zs = self.ode(z0, t_1d)
        hs = self.l2h(zs)
        xs = self.h2o(hs)

        return xs

class ODEVAE(nn.Module):
    def __init__(self, output_dim, hidden_dim, latent_dim, encoder='gru'):
        super(ODEVAE, self).__init__()
        self.output_dim = output_dim
        self.hidden_dim = hidden_dim
        self.latent_dim = latent_dim
        self.encoder = encoder

        self.rnn_encoder = RNNEncoder(output_dim, hidden_dim, latent_dim, encoder=encoder)
        self.neural_decoder = NeuralODEDecoder(output_dim, hidden_dim, latent_dim)

    def forward(self, x, t_encoder, t_decoder, MAP=False):
        mu, logvar = self.rnn_encoder(x, t_encoder)
        if MAP:
            z = mu
        else:
            z = reparameterize(mu, logvar)
        # x_p = self.neural_decoder(z, t).permute(1, 0, 2)
        x_p = self.neural_decoder(z, t_decoder)
        return x_p, z, mu, logvar

def vae_loss_function(device, x_p, x, z, mu, logvar):
    reconstruction_loss = 0.5 * ((x - x_p)**2).sum(-1).sum(0)
    kl_loss = -0.5 * torch.sum(1 + logvar - mu**2 - torch.exp(logvar), -1)

    loss = reconstruction_loss + kl_loss
    loss = torch.mean(loss)

    return loss

def mape(device, y_true, y_pred):
    return torch.mean(torch.abs((y_pred - y_true)) / y_true) * 100

def differentiable_smape(device, y_true, y_pred, epsilon=0.1):
    constant_and_epsilon = torch.tensor(0.5 + epsilon).repeat(y_true.shape).to(device)
    summ = torch.maximum(torch.abs(y_true) + torch.abs(y_pred) + epsilon, constant_and_epsilon)
    smape = (torch.abs(y_pred - y_true) / summ * 2.0)

    return torch.mean(smape)

def rounded_smape(device, y_true, y_pred):
    y_true_copy = torch.round(y_true).type(torch.IntTensor)
    y_pred_copy = torch.round(y_pred).type(torch.IntTensor)
    summ = torch.abs(y_true) + torch.abs(y_pred)
    smape = torch.where(summ == 0, torch.zeros_like(summ), torch.abs(y_pred_copy - y_true_copy) / summ)

    return torch.mean(smape)

def kaggle_smape(device, y_true, y_pred):
    summ = torch.abs(y_true) + torch.abs(y_pred)
    smape = torch.where(summ == 0, torch.zeros_like(summ), torch.abs(y_pred - y_true) / summ)

    return 200 * torch.mean(smape)

def mae(device, y_true, y_pred, mult_factor=1):
    y_true_log = torch.log1p(y_true)
    y_pred_log = torch.log1p(y_pred)
    error = torch.abs(y_true_log - y_pred_log) * mult_factor

    return torch.mean(error)

def mse(device, y_true, y_pred, mult_factor=1):
    y_true_log = torch.log1p(y_true)
    y_pred_log = torch.log1p(y_pred)
    error = torch.pow(y_true_log - y_pred_log, 2) * mult_factor

    return torch.mean(error)

# def train_smape_loss(device, y_true, y_pred):
#     mask = torch.isfinite(y_true).to(device)
#     weight_mask = mask.type(torch.FloatTensor)

#     return differentiable_smape(device, y_true, y_pred)

# def train_mae_loss(device, y_true, y_pred):
#     mask = torch.isfinite(y_true).to(device)
#     weight_mask = mask.type(torch.FloatTensor)

#     return mae(device, y_true, y_pred)

# def test_smape_loss(device, y_true, y_pred):
#     mask = torch.isfinite(y_true).to(device)
#     weight_mask = mask.type(torch.FloatTensor)

#     return kaggle_smape(device, y_true, y_pred)
