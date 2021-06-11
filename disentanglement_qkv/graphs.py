# This file defines the links between variables in the inference and generation networks of the PoS_tagging task
from itertools import chain

import torch.nn as nn

from components.links import CoattentiveTransformerLink, ConditionalCoattentiveTransformerLink, LastStateMLPLink, \
    LSTMLink, ConditionalCoattentiveQKVTransformerLink, MLPLink
from disentanglement_qkv.variables import *


def get_structured_auto_regressive_indep_graph(h_params, word_embeddings):
    xin_size, zin_size = h_params.embedding_dim, h_params.z_size
    xout_size, zout_size = h_params.vocab_size, h_params.z_size
    z_repnet = None
    n_lvls = len(h_params.n_latents)
    lv_size_props = [lv_n/max(h_params.n_latents) for lv_n in h_params.n_latents]
    z_sizes = [int(zin_size*lv_size_prop) for lv_size_prop in lv_size_props]
    # Generation
    x_gen, xprev_gen = XGen(h_params, word_embeddings), XPrevGen(h_params, word_embeddings, has_rep=False)
    z_gens = [ZGeni(h_params, z_repnet, i, allow_prior=(i == 0)) for i in range(n_lvls)]
    zs_xprev_to_x = ConditionalCoattentiveTransformerLink(xin_size, zout_size,
                                                          xout_size,h_params.decoder_l, Categorical.parameter_activations,
                                                          word_embeddings, highway=h_params.highway, sbn=None,
                                                          dropout=h_params.dropout, n_mems=sum(h_params.n_latents),
                                                          memory=[z.name for z in z_gens], targets=['x_prev'],
                                                          nheads=4, bidirectional=False,
                                                          mem_size=int(z_sizes[0]/h_params.n_latents[0]),
                                                          minimal_enc=h_params.minimal_enc)
    z_prior = [CoattentiveTransformerLink(z_sizes[i], int(h_params.decoder_h*lv_size_props[i+1]), z_sizes[i+1], h_params.decoder_l,
                                          Gaussian.parameter_activations, nheads=4, sequence=None,
                                          memory=[z_gens[i].name],
                                          n_mems=h_params.n_latents[i], mem_size=int(z_sizes[i]/h_params.n_latents[i]),
                                          highway=h_params.highway, dropout=h_params.dropout,
                                          n_targets=h_params.n_latents[i+1])
               for i in range(n_lvls-1)]
    number_parameters  = sum(p.numel() for p in zs_xprev_to_x.parameters() if p.requires_grad)
    print("reconstruction net size:", "{0:05.2f} M".format(number_parameters/1e6))
    print("prior net sizes:")
    for i in range(len(z_prior)):
        number_parameters = sum(p.numel() for p in z_prior[i].parameters() if p.requires_grad)
        print("{0:05.2f} M".format(number_parameters/1e6))

    # Inference
    x_inf, z_infs = XInfer(h_params, word_embeddings, has_rep=False), [ZInferi(h_params, z_repnet, i) for i in
                                                                       range(n_lvls)]
    z_posterior = [CoattentiveTransformerLink(xin_size, int(lv_size_props[i]*h_params.encoder_h),
                                              z_sizes[i], h_params.encoder_l, Gaussian.parameter_activations, nheads=4,
                                              sequence=['x'], memory=None,
                                              n_mems=sum(h_params.n_latents[i+1:n_lvls]) or None,
                                              highway=h_params.highway, dropout=h_params.dropout,
                                              n_targets=h_params.n_latents[i]) for i in range(n_lvls)]
    infer_edges = [nn.ModuleList([x_inf, z_posti, z_infi]) for z_posti, z_infi in zip(z_posterior, z_infs)]
    # for retrocompatibility:
    infer_edges = [infer_edges[0]]+infer_edges[n_lvls:]+infer_edges[1:n_lvls]
    gen_edges = [nn.ModuleList([z_gens[i], z_prior[i], z_gens[i+1]]) for i in range(n_lvls-1)]+\
                [nn.ModuleList([var, zs_xprev_to_x, x_gen]) for var in z_gens+[xprev_gen]]


    return {'infer': nn.ModuleList(infer_edges),
            'gen':   nn.ModuleList(gen_edges)}, None, x_gen


def get_qkv_graph(h_params, word_embeddings):
    xin_size, zin_size = h_params.embedding_dim, h_params.z_size
    xout_size, zout_size = h_params.vocab_size, h_params.z_size
    n_keys = h_params.n_keys
    zstin_size, zstout_size = int(h_params.z_size/max(h_params.n_latents)), \
                              int(h_params.z_size/max(h_params.n_latents))
    z_repnet = None
    n_lvls = len(h_params.n_latents)
    lv_size_props = [lv_n/max(h_params.n_latents) for lv_n in h_params.n_latents]
    z_sizes = [int(zin_size*lv_size_prop) for lv_size_prop in lv_size_props]
    # Generation
    x_gen, xprev_gen = XGen(h_params, word_embeddings), XPrevGen(h_params, word_embeddings, has_rep=False)
    z_gens = [ZGeni(h_params, z_repnet, i, allow_prior=(i == 0)) for i in range(n_lvls)]
    zst_gen = ZStGen(h_params)
    zst_zs_xprev_to_x = ConditionalCoattentiveQKVTransformerLink(xin_size, zout_size, xout_size, h_params.decoder_l,
                                                                 Categorical.parameter_activations, word_embeddings,
                                                                 highway=h_params.highway, sbn=None,
                                                                 dropout=h_params.dropout, n_mems=sum(h_params.n_latents),
                                                                 memory=[z.name for z in z_gens], targets=['x_prev'],
                                                                 key=['zs'], nheads=4, bidirectional=False,
                                                                 mem_size=int(z_sizes[0]/h_params.n_latents[0]),
                                                                 minimal_enc=h_params.minimal_enc, n_keys=n_keys)
    z_prior = [CoattentiveTransformerLink(z_sizes[i], int(h_params.decoder_h*lv_size_props[i+1]), z_sizes[i+1],
                                          h_params.decoder_l, Gaussian.parameter_activations, nheads=4, sequence=None,
                                          memory=[z_gens[i].name], n_mems=h_params.n_latents[i],
                                          mem_size=int(z_sizes[i]/h_params.n_latents[i]), highway=h_params.highway,
                                          dropout=h_params.dropout, n_targets=h_params.n_latents[i+1])
               for i in range(n_lvls-1)]
    number_parameters = sum(p.numel() for p in zst_zs_xprev_to_x.parameters() if p.requires_grad)
    print("reconstruction net size:", "{0:05.2f} M".format(number_parameters/1e6))
    print("prior net sizes:")
    for i in range(len(z_prior)):
        number_parameters = sum(p.numel() for p in z_prior[i].parameters() if p.requires_grad)
        print("{0:05.2f} M".format(number_parameters/1e6))

    # Inference
    x_inf, z_infs, zst_inf = XInfer(h_params, word_embeddings, has_rep=False), [ZInferi(h_params, z_repnet, i) for i in
                                                                       range(n_lvls)], ZStInfer(h_params)
    z_posterior = [CoattentiveTransformerLink(xin_size, int(lv_size_props[i]*h_params.encoder_h),
                                              z_sizes[i], h_params.encoder_l, Gaussian.parameter_activations, nheads=4,
                                              sequence=['x'], memory=None,
                                              n_mems=sum(h_params.n_latents[i+1:n_lvls]) or None,
                                              highway=h_params.highway, dropout=h_params.dropout,
                                              n_targets=h_params.n_latents[i]) for i in range(n_lvls)]
    x_to_zst = CoattentiveTransformerLink(xin_size, h_params.encoder_h, zstout_size, h_params.encoder_l,
                                          Gaussian.parameter_activations, nheads=4, sequence=['x'],
                                          dropout=h_params.dropout, n_targets=1)
    infer_edges = [nn.ModuleList([x_inf, z_posti, z_infi]) for z_posti, z_infi in zip(z_posterior, z_infs)]+\
                  [nn.ModuleList([x_inf, x_to_zst, zst_inf])]

    gen_edges = [nn.ModuleList([z_gens[i], z_prior[i], z_gens[i+1]]) for i in range(n_lvls-1)] +\
                [nn.ModuleList([var, zst_zs_xprev_to_x, x_gen]) for var in z_gens+[xprev_gen]+[zst_gen]]

    return {'infer': nn.ModuleList(infer_edges), 'gen':   nn.ModuleList(gen_edges)}, None, x_gen


def get_vanilla_graph(h_params, word_embeddings):
    xin_size, zin_size = h_params.embedding_dim, h_params.z_size
    xout_size, zout_size = h_params.vocab_size, h_params.z_size
    # Generation
    x_gen, xprev_gen = XGen(h_params, word_embeddings), XPrevGen(h_params, word_embeddings, has_rep=False)
    z_gen = ZGeni(h_params, None, 0, allow_prior=True)
    z_xprev_to_x = LSTMLink(xin_size+zin_size, h_params.decoder_h, xout_size, h_params.decoder_l,
                            Categorical.parameter_activations, dropout=h_params.dropout)

    # Inference
    x_inf, z_inf = XInfer(h_params, word_embeddings, has_rep=False), ZInferi(h_params, None, 0)
    x_to_z = LSTMLink(xin_size, h_params.encoder_h, zout_size, h_params.encoder_l, Gaussian.parameter_activations,
                      dropout=h_params.dropout, last_state=True, bidirectional=True)

    return {'infer': nn.ModuleList([nn.ModuleList([x_inf, x_to_z, z_inf])]),
            'gen':   nn.ModuleList([nn.ModuleList([xprev_gen, z_xprev_to_x, x_gen]),
                                   nn.ModuleList([z_gen, z_xprev_to_x, x_gen])])}, None, x_gen


def get_hqkv_graph(h_params, word_embeddings):
    xin_size, zin_size = h_params.embedding_dim, h_params.z_size
    xout_size, zout_size = h_params.vocab_size, h_params.z_size
    n_keys = h_params.n_keys
    zstin_size, zstout_size = int(h_params.z_size/max(h_params.n_latents)), \
                              int(h_params.z_size/max(h_params.n_latents))
    z_repnet = None
    n_lvls = len(h_params.n_latents)
    lv_size_props = [lv_n/max(h_params.n_latents) for lv_n in h_params.n_latents]
    z_sizes = [int(zin_size*lv_size_prop) for lv_size_prop in lv_size_props]
    # Generation
    x_gen, xprev_gen = XGen(h_params, word_embeddings), XPrevGen(h_params, word_embeddings, has_rep=False)
    z_gens = [ZGeni(h_params, z_repnet, i, allow_prior=False) for i in range(n_lvls)]
    zst_gen, zg_gen = ZStGen(h_params, allow_prior=False), ZGGen(h_params, allow_prior=True)
    zst_zs_xprev_to_x = ConditionalCoattentiveQKVTransformerLink(xin_size, zout_size, xout_size, h_params.decoder_l,
                                                                 Categorical.parameter_activations, word_embeddings,
                                                                 highway=h_params.highway, sbn=None,
                                                                 dropout=h_params.dropout, n_mems=sum(h_params.n_latents),
                                                                 memory=[z.name for z in z_gens], targets=['x_prev'],
                                                                 key=['zs'], nheads=4, bidirectional=False,
                                                                 mem_size=int(z_sizes[0]/h_params.n_latents[0]),
                                                                 minimal_enc=h_params.minimal_enc, n_keys=n_keys)
    z_prior = [CoattentiveTransformerLink(z_sizes[i], int(h_params.decoder_h*lv_size_props[i+1]), z_sizes[i+1],
                                          h_params.decoder_l, Gaussian.parameter_activations, nheads=4, sequence=None,
                                          memory=[z_gens[i].name], n_mems=h_params.n_latents[i],
                                          mem_size=int(z_sizes[i]/h_params.n_latents[i]), highway=h_params.highway,
                                          dropout=h_params.dropout, n_targets=h_params.n_latents[i+1])
               for i in range(n_lvls-1)]

    zg_to_z = MLPLink(zstin_size, h_params.decoder_h, z_sizes[0], h_params.decoder_l, Gaussian.parameter_activations,
                      dropout=h_params.dropout)
    zg_to_zst = MLPLink(zstin_size, h_params.decoder_h, zstout_size, h_params.decoder_l, Gaussian.parameter_activations,
                        dropout=h_params.dropout)
    number_parameters = sum(p.numel() for p in zst_zs_xprev_to_x.parameters() if p.requires_grad)
    print("reconstruction net size:", "{0:05.2f} M".format(number_parameters/1e6))
    print("prior net sizes:")
    for i in range(len(z_prior)):
        number_parameters = sum(p.numel() for p in z_prior[i].parameters() if p.requires_grad)
        print("{0:05.2f} M".format(number_parameters/1e6))

    # Inference
    x_inf, z_infs, zst_inf = XInfer(h_params, word_embeddings, has_rep=False), [ZInferi(h_params, z_repnet, i) for i in
                                                                       range(n_lvls)], ZStInfer(h_params)
    zg_inf = ZGInfer(h_params)
    z_posterior = [CoattentiveTransformerLink(xin_size, int(lv_size_props[i]*h_params.encoder_h),
                                              z_sizes[i], h_params.encoder_l, Gaussian.parameter_activations, nheads=4,
                                              sequence=['x'], memory=None,
                                              n_mems=sum(h_params.n_latents[i+1:n_lvls]) or None,
                                              highway=h_params.highway, dropout=h_params.dropout,
                                              n_targets=h_params.n_latents[i]) for i in range(n_lvls)]
    x_to_zst = CoattentiveTransformerLink(xin_size, h_params.encoder_h, zstout_size, h_params.encoder_l,
                                          Gaussian.parameter_activations, nheads=4, sequence=['x'],
                                          dropout=h_params.dropout, n_targets=1)
    x_to_zg = CoattentiveTransformerLink(xin_size, h_params.encoder_h, zstout_size, h_params.encoder_l,
                                          Gaussian.parameter_activations, nheads=4, sequence=['x'],
                                          dropout=h_params.dropout, n_targets=1)
    infer_edges = [nn.ModuleList([x_inf, z_posti, z_infi]) for z_posti, z_infi in zip(z_posterior, z_infs)]+\
                  [nn.ModuleList([x_inf, x_to_zst, zst_inf])]+[nn.ModuleList([x_inf, x_to_zg, zg_inf])]

    gen_edges = [nn.ModuleList([z_gens[i], z_prior[i], z_gens[i+1]]) for i in range(n_lvls-1)] +\
                [nn.ModuleList([var, zst_zs_xprev_to_x, x_gen]) for var in z_gens+[xprev_gen]+[zst_gen]]+ \
                [nn.ModuleList([zg_gen, zg_to_z, z_gens[0]])]+[nn.ModuleList([zg_gen, zg_to_zst, zst_gen])]

    return {'infer': nn.ModuleList(infer_edges), 'gen':   nn.ModuleList(gen_edges)}, None, x_gen

