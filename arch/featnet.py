'''Implicit feature network

'''

import logging

import torch
import torch.nn.functional as F

from .ali import build_extra_networks, network_routine as ali_network_routine
from .gan import get_positive_expectation, get_negative_expectation, apply_gradient_penalty, generator_loss
from .modules.fully_connected import FullyConnectedNet
from .vae import update_encoder_args, update_decoder_args


logger = logging.getLogger('cortex.arch' + __name__)

resnet_encoder_args_ = dict(dim_h=64, batch_norm=True, f_size=3, n_steps=3)
resnet_decoder_args_ = dict(dim_h=64, batch_norm=True, f_size=3, n_steps=3)
mnist_encoder_args_ = dict(dim_h=64, batch_norm=True, f_size=5, pad=2, stride=2, min_dim=7)
mnist_decoder_args_ = dict(dim_h=64, batch_norm=True, f_size=4, pad=1, stride=2, n_steps=2)
convnet_encoder_args_ = dict(dim_h=64, batch_norm=True, n_steps=3)
convnet_decoder_args_ = dict(dim_h=64, batch_norm=True, n_steps=3)


def shape_noise(Y_P, U, noise_type, epsilon=1e-6):
    if noise_type == 'hypercubes':
        pass
    elif noise_type == 'unitsphere':
        Y_P = Y_P / (torch.sqrt((Y_p ** 2).sum(1, keepdim=True)) + epsilon)
    elif noise_type == 'unitball':
        Y_P = Y_P / (torch.sqrt((Y_P ** 2).sum(1, keepdim=True)) + epsilon) * U.expand(Y_P.size())
    else:
        raise ValueError

    return Y_P


def encode(models, X, Y_P, output_nonlin=False, noise_type='hypercubes'):
    encoder = models['encoder']

    if isinstance(encoder, (tuple, list)) and len(encoder) == 3:
        encoder, topnet, revnet = encoder
    else:
        topnet = None

    Z_Q = encoder(X)
    if output_nonlin:
        if noise_type == 'hypercubes':
            Z_Q = F.sigmoid(Z_Q)
        elif noise_type == 'unitsphere':
            Z_Q = Z_Q / (torch.sqrt((Z_Q ** 2).sum(1, keepdim=True)) + 1e-6)
        elif noise_type == 'unitball':
            Z_Q = F.tanh(Z_Q)

    if topnet is not None:
        Y_Q = topnet(Z_Q)
        Z_P = revnet(Y_P)
    else:
        Y_Q = None
        Z_P = Y_P

    return Z_P, Z_Q, Y_Q


def score(models, Z_P, Z_Q, measure, Y_P=None, Y_Q=None, key='discriminator'):
    discriminator = models[key]
    if Y_Q is not None:
        Z_Q = torch.cat([Y_Q, Z_Q], 1)
        Z_P = torch.cat([Y_P, Z_P], 1)

    Q_samples = discriminator(Z_Q)
    P_samples = discriminator(Z_P)

    E_pos = get_positive_expectation(P_samples, measure)
    E_neg = get_negative_expectation(Q_samples, measure)
    return E_pos, E_neg, P_samples, Q_samples


def get_results(P_samples, Q_samples, E_pos, E_neg, measure, results=None):
    if results is not None:
        results.update(Scores=dict(Ep=P_samples.mean().data[0], Eq=Q_samples.mean().data[0]))
        results['{} distance'.format(measure)] = (E_pos - E_neg).data[0]


def visualize(Z_Q, P_samples, Q_samples, X, T, Y_Q=None, viz=None):
    if viz is not None:
        if Y_Q is not None:
            viz.add_scatter(Z_Q, labels=T.data, name='intermediate values')
            viz.add_scatter(Y_Q, labels=T.data, name='latent values')
        else:
            viz.add_scatter(Z_Q, labels=T.data, name='latent values')
        viz.add_image(X, name='ground truth')
        viz.add_histogram(dict(fake=Q_samples.view(-1).data, real=P_samples.view(-1).data), name='discriminator output')


def encoder_routine(data, models, losses, results, viz, measure=None, noise_type='hypercubes',
                    output_nonlin=False, generator_loss_type=None, **kwargs):
    X, Y_P, T, U = data.get_batch('images', 'y', 'targets', 'u')
    Y_P = shape_noise(Y_P, U, noise_type)

    Z_P, Z_Q, Y_Q = encode(models, X, Y_P, output_nonlin=output_nonlin, noise_type=noise_type)
    E_pos, E_neg, P_samples, Q_samples = score(models, Z_P, Z_Q, measure, Y_P=Y_P, Y_Q=Y_Q)
    get_results(P_samples, Q_samples, E_pos, E_neg, measure, results=results)
    visualize(Z_Q, P_samples, Q_samples, X, T, Y_Q=Y_Q, viz=viz)

    encoder_loss = generator_loss(Q_samples, measure, loss_type=generator_loss_type)
    losses.update(encoder=encoder_loss)


def discriminator_routine(data, models, losses, results, viz, penalty_amount=0., measure=None, noise_type='hypercubes',
                          output_nonlin=False, **kwargs):
    X, Y_P, U = data.get_batch('images', 'y', 'u')
    Y_P = shape_noise(Y_P, U, noise_type)

    Z_P, Z_Q, Y_Q = encode(models, X, Y_P, output_nonlin=output_nonlin, noise_type=noise_type)
    E_pos, E_neg, _, _ = score(models, Z_P, Z_Q, measure, Y_P=Y_P, Y_Q=Y_Q)
    losses.update(discriminator=E_pos - E_neg)

    if Y_Q is not None:
        Z_Q = torch.cat([Y_Q, Z_Q], 1)
        Z_P = torch.cat([Y_P, Z_P], 1)

    apply_gradient_penalty(data, models, losses, results, inputs=(Z_P, Z_Q), model='discriminator',
                           penalty_amount=penalty_amount)


def network_routine(data, models, losses, results, viz, **kwargs):
    ali_network_routine(data, models, losses, results, viz, encoder_key='encoder', **kwargs)


# Cortex ===============================================================================================================


def setup(model=None, data=None, routines=None, **kwargs):
    noise = routines['discriminator']['noise']
    noise_type = routines['discriminator']['noise_type']
    if noise_type in ('unitsphere', 'unitball'):
        noise = 'normal'
    data['noise_variables'] = dict(y=(noise, model['dim_noise']))
    data['noise_variables']['u'] = ('uniform', 1)
    routines['encoder'].update(**routines['discriminator'])


def build_encoder(models, x_shape, dim_z, Encoder, use_topnet=False, dim_top=None, **encoder_args):
    logger.debug('Forming encoder with class {} and args: {}'.format(Encoder, encoder_args))
    encoder = Encoder(x_shape, dim_out=dim_z, **encoder_args)

    if use_topnet:
        topnet = FullyConnectedNet(dim_z, dim_h=dim_top[::-1], dim_out=dim_top, batch_norm=True)
        revnet = FullyConnectedNet(dim_top, dim_h=dim_top, dim_out=dim_z, batch_norm=True)
        encoder = [encoder, topnet, revnet]

    models.update(encoder=encoder)


def build_discriminator(models, dim_in, key='discriminator'):
    discriminator = FullyConnectedNet(dim_in, dim_h=[2048, 1028, 512], dim_out=1, batch_norm=False)
    models[key] = discriminator


def build_model(data, models, model_type='convnet', use_topnet=False, dim_noise=16, dim_embedding=16,
                encoder_args=None, decoder_args=None):

    if not use_topnet:
        dim_embedding = dim_noise
        dim_d = dim_embedding
    else:
        dim_d = dim_embedding + dim_noise

    x_shape = data.get_dims('x', 'y', 'c')
    dim_l = data.get_dims('labels')

    Encoder, encoder_args = update_encoder_args(x_shape, model_type=model_type, encoder_args=encoder_args)
    Decoder, decoder_args = update_decoder_args(x_shape, model_type=model_type, decoder_args=decoder_args)

    build_encoder(models, x_shape, dim_noise, Encoder, use_topnet=use_topnet, dim_top=dim_noise, **encoder_args)
    build_discriminator(models, dim_d)
    build_extra_networks(models, x_shape, dim_embedding, dim_l, Decoder, **decoder_args)


ROUTINES = dict(discriminator=discriminator_routine, encoder=encoder_routine, nets=network_routine)


DEFAULT_CONFIG = dict(
    data=dict(batch_size=dict(train=64, test=1028), skip_last_batch=True),
    optimizer=dict(
        optimizer='Adam',
        learning_rate=dict(discriminator=1e-4, nets=1e-4, encoder=1e-4),
        updates_per_model=dict(discriminator=1, nets=1, encoder=1)),
    model=dict(model_type='convnet', dim_embedding=64, dim_noise=64, encoder_args=None, use_topnet=False),
    routines=dict(discriminator=dict(measure='JSD', penalty_amount=1., noise_type='hypercubes', noise='uniform'),
                  encoder=dict(generator_loss_type='non-saturating'),
                  nets=dict()),
    train=dict(epochs=500, archive_every=10)
)