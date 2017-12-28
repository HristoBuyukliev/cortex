'''Simple GAN model

'''

import logging
import math

import torch
from torch import autograd
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F

#from resnets import ResEncoder as Discriminator
#from resnets import ResDecoder as Generator

from conv_decoders import SimpleConvDecoder as Generator
from convnets import SimpleConvEncoder as Discriminator

logger = logging.getLogger('cortex.models' + __name__)

GLOBALS = {'DIM_X': None, 'DIM_Y': None, 'DIM_C': None, 'DIM_L': None, 'DIM_Z': None}

sw = 1

if sw == 0:
    discriminator_args_ = dict(dim_h=64, batch_norm=True, f_size=3, n_steps=3)
    generator_args_ = dict(dim_h=64, batch_norm=True, f_size=3, n_steps=3)

elif sw == 1:

    discriminator_args_ = dict(dim_h=64, batch_norm=True, f_size=5, pad=2, stride=2, min_dim=7,
                            nonlinearity='LeakyReLU')
    generator_args_ = dict(dim_h=64, batch_norm=True, f_size=4, pad=1, stride=2, n_steps=2)

else:
    discriminator_args_ = dict(dim_h=64, batch_norm=True, min_dim=4, nonlinearity='LeakyReLU')
    generator_args_ = dict(dim_h=64, batch_norm=True, n_steps=3)

DEFAULTS = dict(
    data=dict(batch_size=64,
              noise_variables=dict(z=('normal', 64)),
              test_batch_size=64),
    optimizer=dict(
        optimizer='Adam',
        learning_rate=1e-4,
    ),
    model=dict(discriminator_args=discriminator_args_, generator_args=generator_args_),
    procedures=dict(measure='proxy_gan', penalty=False, boundary_seek=False),
    train=dict(
        epochs=200,
        summary_updates=100,
        archive_every=10
    )
)


def f_divergence(measure, real_out, fake_out, boundary_seek=False):
    log_2 = math.log(2.)

    if measure in ('gan', 'proxy_gan'):
        r = -F.softplus(-real_out)
        f = F.softplus(-fake_out) + fake_out
        w = torch.exp(fake_out)
        b = fake_out ** 2

    elif measure == 'jsd':
        r = log_2 - F.softplus(-real_out)
        f = F.softplus(-fake_out) + fake_out + log_2
        w = torch.exp(fake_out)
        b = fake_out ** 2

    elif measure == 'xs':
        r = real_out ** 2
        f = -0.5 * ((torch.sqrt(fake_out ** 2) + 1.) ** 2)
        w = 0.5 * (1. - 1. / torch.sqrt(fake_out ** 2))
        b = (fake_out / 2.) ** 2

    elif measure == 'kl':
        r = real_out + 1.
        f = torch.exp(fake_out)
        w = torch.exp(fake_out)
        b = fake_out ** 2

    elif measure == 'rkl':
        r = -torch.exp(-real_out)
        f = fake_out - 1.
        w = torch.exp(fake_out)
        b = fake_out ** 2

    elif measure == 'dv':
        r = real_out
        f = torch.log(torch.exp(fake_out))
        w = torch.exp(fake_out)
        b = fake_out ** 2

    elif measure == 'sh':
        r = 1. - torch.exp(-real_out)
        f = torch.exp(fake_out) - 1.
        w = torch.exp(fake_out)
        b = fake_out ** 2

    elif measure == 'w':
        r = real_out
        f = fake_out
        w = fake_out
        b = Variable(torch.Tensor([0.]).float()).cuda()

    else:
        raise NotImplementedError(measure)
    d_loss = f.mean() - r.mean()

    if boundary_seek:
        g_loss = torch.mean(b)

    elif measure == 'proxy_gan':
        g_loss = torch.mean(F.softplus(-fake_out))

    else:
        g_loss = -torch.mean(f)

    return d_loss, g_loss, r, f, w, b


def apply_penalty(inputs, discriminator, real, fake, measure, penalty_type='gradient_norm'):
    real = Variable(real.data.cuda(), requires_grad=True)
    fake = Variable(fake.data.cuda(), requires_grad=True)
    real_out = discriminator(real)
    fake_out = discriminator(fake)

    if penalty_type == 'gradient_norm':

        g_r = autograd.grad(outputs=real_out, inputs=real, grad_outputs=torch.ones(real_out.size()).cuda(),
                            create_graph=True, retain_graph=True, only_inputs=True)[0]

        g_f = autograd.grad(outputs=fake_out, inputs=fake, grad_outputs=torch.ones(fake_out.size()).cuda(),
                            create_graph=True, retain_graph=True, only_inputs=True)[0]

        if measure in ('gan', 'proxy_gan', 'jsd'):
            g_r = (1. - F.sigmoid(real_out)) ** 2 * (g_r ** 2).sum(1).sum(1).sum(1)
            g_f = F.sigmoid(fake_out) ** 2 * (g_f ** 2).sum(1).sum(1).sum(1)

        else:
            g_r = (g_r ** 2).sum(1).sum(1).sum(1)
            g_f = (g_f ** 2).sum(1).sum(1).sum(1)

        g_p = 0.5 * (g_r.mean() + g_f.mean())

        return g_p

    elif penalty_type == 'interpolate':
        if 'e' not in inputs:
            raise ValueError('You must initiate a uniform random variable `e` to use interpolation')
        epsilon = inputs['e'].view(-1, 1, 1, 1)
        interpolations = Variable(((1. - epsilon) * fake + epsilon * real[:fake.size()[0]]).data.cuda(),
                                  requires_grad=True)

        mid_out = discriminator(interpolations)
        g = autograd.grad(outputs=mid_out, inputs=interpolations, grad_outputs=torch.ones(mid_out.size()).cuda(),
                          create_graph=True, retain_graph=True, only_inputs=True)[0]
        s = (g ** 2).sum(1).sum(1).sum(1)
        g_p = ((torch.sqrt(s) - 1.) ** 2)
        return g_p.mean()

    else:
        raise NotImplementedError(penalty_type)

def gan(nets, inputs, measure=None, boundary_seek=False, penalty=None):
    Z = inputs['z']
    X = inputs['images']
    #X = 0.5 * (X + 1.)

    discriminator = nets['discriminator']
    generator = nets['generator']
    gen_out = generator(Z, nonlinearity=F.tanh)

    real_out = discriminator(X)
    fake_out = discriminator(gen_out)

    d_loss, g_loss, r, f, w, b = f_divergence(measure, real_out, fake_out, boundary_seek=boundary_seek)

    results = dict(g_loss=g_loss.data[0], d_loss=d_loss.data[0], boundary=torch.mean(b).data[0],
                   real=torch.mean(r).data[0], fake=torch.mean(f).data[0], w=torch.mean(w).data[0])
    samples = dict(images=dict(generated=0.5 * (gen_out.data + 1.), real=0.5 * (inputs['images'].data + 1.)))
    #samples = dict(images=dict(generated=gen_out.data, real=inputs['images'].data))
    if penalty:
        p_term = apply_penalty(inputs, discriminator, X, gen_out, measure)

        d_loss += penalty * torch.mean(p_term)
        results['gradient penalty'] = torch.mean(p_term).data[0]

    return dict(generator=g_loss, discriminator=d_loss), results, samples, 'boundary'


def build_model(loss=None, discriminator_args=None, generator_args=None):
    discriminator_args = discriminator_args or {}
    generator_args = generator_args or {}

    shape = (DIM_X, DIM_Y, DIM_C)

    discriminator = Discriminator(shape, dim_out=1, **discriminator_args)
    generator = Generator(shape, dim_in=64, **generator_args)
    logger.debug(discriminator)
    logger.debug(generator)

    return dict(generator=generator, discriminator=discriminator), gan


