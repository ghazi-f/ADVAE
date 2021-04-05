from torch.utils.tensorboard import SummaryWriter
import torch
import numpy as np
from tqdm import tqdm
import pandas as pd

from disentanglement_transformer.h_params import *
from components.bayesnets import BayesNet
from components.criteria import Supervision, ELBo
from components.latent_variables import MultiCategorical
import spacy
from allennlp.predictors.predictor import Predictor
import matplotlib.pyplot as plt
import seaborn as sns
sns.set_style("ticks", {"xtick.major.color": 'white', "ytick.major.color": 'white'})

nlp = spacy.load("en_core_web_sm")

predictor = Predictor.from_path("https://storage.googleapis.com/allennlp-public-models/openie-model.2020.03.26.tar.gz")


# =============================================== NORMALIZATION MODEL CLASS ============================================

class NormalizationModel(nn.Module):
    def __init__(self, vocab_index, char_index, h_params, graph, generated_v, supervised_lv):
        super(NormalizationModel, self).__init__()

        self.h_params = h_params
        # Setting up categorical variable indexes
        self.index = {'w': vocab_index, 'c': char_index}
        self.generated_v = generated_v
        self.supervised_v = supervised_lv
        # Instanciating inference and generation networks
        self.infer_bn = BayesNet(graph['infer'])
        self.infer_last_states = None
        self.infer_last_states_test = None
        self.gen_bn = BayesNet(graph['gen'])
        self.gen_last_states = None
        self.gen_last_states_test = None
        self.step = 0

    def forward(self, samples, eval=False, prev_states=None, force_iw=None, substitute_gen_vals=None,
                plant_gen_posteriors=None, only_gen=False):
        # Just propagating values through the bayesian networks to get summaries
        if prev_states:
            infer_prev, gen_prev = prev_states
        else:
            infer_prev, gen_prev = None, None

        #                          ----------- Unsupervised Forward/Backward ----------------
        # Forward pass
        if not only_gen:
            infer_inputs = {'c': samples['c'], 'wid': samples['wid']}
            infer_prev = self.infer_bn(infer_inputs, n_iw=self.h_params.testing_iw_samples, eval=eval,
                                       prev_states=infer_prev, force_iw=force_iw, complete=True)
        w_prev = [v[..., :-1, :] for k, v in self.infer_bn.variables_hat.items() if k.name == 'w'][0]
        go_emb = torch.ones([*w_prev.shape[:-2]], device=self.h_params.device).long() * self.index['w'].stoi['<go>']
        go_emb = self.infer_bn.name_to_v['wid'].embedding(go_emb.unsqueeze(-1))
        w_prev = torch.cat([go_emb, w_prev],
                           dim=-2)
        gen_inputs = {**{k.name: v for k, v in self.infer_bn.variables_hat.items()},
                      **{'c': samples['c'],  #'c_prev': samples['c_prev'],
                         'w_prev': w_prev}, **(substitute_gen_vals or {})}
        gen_inputs.pop('wid', None)
        if force_iw:
            gen_inputs = self._harmonize_input_shapes(gen_inputs, self.h_params.testing_iw_samples)
        if self.step < self.h_params.anneal_kl[0]:
            gen_prev = self.gen_bn(gen_inputs, target=self.generated_v, eval=eval, prev_states=gen_prev,
                                   complete=True, plant_posteriors=plant_gen_posteriors)
        else:
            gen_prev = self.gen_bn(gen_inputs, eval=eval, prev_states=gen_prev, complete=True,
                                   plant_posteriors=plant_gen_posteriors)

        if self.h_params.contiguous_lm:
            return infer_prev, gen_prev
        else:
            return None, None

    def _harmonize_input_shapes(self, gen_inputs, n_iw):
        # This function repeats inputs to the generation network so that they all have the same shape
        max_n_dims = max([val.ndim for val in gen_inputs.values()])
        for k, v in gen_inputs.items():
            lv_obj = self.gen_bn.name_to_v[k]
            actual_v_ndim = v.ndim + (1 if v.dtype == torch.long and
                                      not (isinstance(lv_obj, MultiCategorical) or
                                           (isinstance(lv_obj, Categorical) and lv_obj.sub_lvl_size is not None))
                                      else 0)
            for _ in range(max_n_dims-actual_v_ndim):
                expand_arg = [n_iw]+list(gen_inputs[k].shape)
                gen_inputs[k] = gen_inputs[k].unsqueeze(0).expand(expand_arg)
        return gen_inputs


# =========================================== TRAINING HANDLING CLASSES ================================================

class BaseTrainingHandler(nn.Module, metaclass=abc.ABCMeta):
    def __init__(self, vocab_index, char_index, h_params, autoload=True, wvs=None):
        super(BaseTrainingHandler, self).__init__()

        self.h_params = h_params
        self.word_embeddings = nn.Embedding(h_params.w_vocab_size, h_params.w_embedding_dim)
        self.char_embeddings = nn.Embedding(h_params.c_vocab_size, h_params.c_embedding_dim)
        self.y_embeddings = nn.Embedding(2, h_params.y_embedding_dim)
        nn.init.uniform_(self.word_embeddings.weight, -1., 1.)
        nn.init.uniform_(self.char_embeddings.weight, -1., 1.)
        if wvs is not None:
            self.word_embeddings.weight.data.copy_(wvs)
            # self.word_embeddings.weight.requires_grad = False

        # Getting vertices
        vertices, gen_lv, sup_lv = h_params.graph_generator(h_params, self.char_embeddings, self.word_embeddings,
                                                            self.y_embeddings)

        # Instanciating inference and generation networks
        self.clean_model = NormalizationModel(vocab_index, char_index, h_params, vertices['clean'], gen_lv['clean'],
                                              sup_lv['clean'])
        self.noise_model = NormalizationModel(vocab_index, char_index, h_params, vertices['noise'], gen_lv['noise'],
                                              sup_lv['noise'])

        # Setting up categorical variable indexes
        self.index = {'w': vocab_index, 'c': char_index}

        # The losses
        self.losses = {'unsup_noise': IWLBo(self.noise_model, 1), 'unsup_clean': IWLBo(self.clean_model, 1),
                       'sup_noise': Supervision(self.noise_model, 1), 'sup_clean': Supervision(self.clean_model, 1)}

        # The Optimizer
        self.optimizer = h_params.optimizer(self.parameters(), **h_params.optimizer_kwargs)

        # Getting the Summary writer
        self.writer = SummaryWriter(h_params.viz_path)
        self.step = 0

        # Loading previous checkpoint if auto_load is set to True
        if autoload:
            self.load()

    @abc.abstractmethod
    def opt_step(self, samples):
        pass

    @abc.abstractmethod
    def forward(self, samples, eval=False, prev_states=None, force_iw=None):
        pass

    def _dump_train_viz(self):
        # Dumping gradient norm
        if (self.step % self.h_params.grad_accumulation_steps) == (self.h_params.grad_accumulation_steps - 1):
            for module, name in zip([self.noise_model, self.noise_model.infer_bn, self.noise_model.gen_bn],
                                    ['overall', 'inference', 'generation']):
                grad_norm = 0
                for p in module.parameters():
                    if p.grad is not None:
                        param_norm = p.grad.data.norm(2)
                        grad_norm += param_norm.item() ** 2
                grad_norm = grad_norm ** (1. / 2)
                self.writer.add_scalar('train' + '/' + '_'.join([name, 'grad_norm']), grad_norm, self.step)

        # Getting the interesting metrics: this model's loss and some other stuff that would be useful for diagnosis
        for loss_name, loss in self.losses.items():
            for name, metric in loss.metrics().items():
                self.writer.add_scalar('train'+name+'['+loss_name.split('_')[1]+']', metric, self.step)

    def dump_test_viz(self, complete=False):
        if complete:
            print('Performing complete test')
        # Getting the interesting metrics: this model's loss and some other stuff that would be useful for diagnosis
        for loss_name, loss in self.losses.items():
            for name, metric in loss.metrics().items():
                self.writer.add_scalar('test'+name+'['+loss_name.split('_')[1]+']', metric, self.step)

        summary_dumpers = {'scalar': self.writer.add_scalar, 'text': self.writer.add_text,
                           'image': self.writer.add_image}

        # We limit the generation of these samples to the less frequent "complete" test visualisations because their
        # computational cost may be high, and because the make the log file a lot larger.
        if complete:
            for summary_type, summary_name, summary_data in self.data_specific_metrics():
                summary_dumpers[summary_type]('test' + summary_name, summary_data, self.step)

    def data_specific_metrics(self):
        # this is supposed to output a list of (summary type, summary name, summary data) triplets
        with torch.no_grad():
            summary_triplets = [
                ('text', '/noise_ground_truth',
                 self.decode_to_text(self.noise_model.gen_bn.variables_star[self.noise_model.generated_v])),
                ('text', '/noise_reconstructions',
                 self.decode_to_text(self.noise_model.generated_v.post_params['logits'])),
                ('text', '/clean_ground_truth',
                 self.decode_to_text(self.clean_model.gen_bn.variables_star[self.clean_model.generated_v])),
                ('text', '/clean_reconstructions',
                 self.decode_to_text(self.clean_model.generated_v.post_params['logits'])),
            ]
        return summary_triplets

    def decode_to_text(self, c_hat_params, gen=False):
        # It is assumed that this function is used at test time for display purposes
        # Getting the argmax from the one hot if it's not done
        # /!\ Commented out importance weighting specific measures because I don't plan on using it here
        # while c_hat_params.shape[-1] == self.h_params.c_vocab_size and c_hat_params.ndim > 3:
        #     c_hat_params = c_hat_params.mean(0)
        # while c_hat_params.ndim > 2 and c_hat_params.shape[-1] != self.h_params.vocab_size:
        #     c_hat_params = c_hat_params[0]
        if c_hat_params.shape[-1] == (self.h_params.c_vocab_size*self.h_params.c_max_len):
            c_hat_params = torch.argmax(c_hat_params.view((*c_hat_params.shape[:-1], self.h_params.c_max_len,
                                                           int(c_hat_params.shape[-1]/self.h_params.c_max_len))), dim=-1)
        if c_hat_params.shape[-1] == self.h_params.c_max_len:
            c_hat_params = c_hat_params.reshape((*c_hat_params.shape[:-2],
                                                 c_hat_params.shape[-2]*c_hat_params.shape[-1]))
        assert c_hat_params.ndim == 2, "Mis-shaped generated sequence: {}".format(c_hat_params.shape)
        text = ' |||| '.join([''.join([(' ' if (i % self.h_params.c_max_len == 0) else '')+self.index['c'].itos[char]
                                        for i, char in enumerate(sen)])  # .split('<eos>')[0]
                              for sen in c_hat_params]).replace('<pad>', ' ').replace('_unk', '<?>')
        text = ' '.join([w.split('<eow>')[0] for w in text.split(' ')]).replace('<eos>', '\n')

        return text

    def _harmonize_input_shapes(self, gen_inputs, n_iw):
        # This function repeats inputs to the generation network so that they all have the same shape
        max_n_dims = max([val.ndim for val in gen_inputs.values()])
        for k, v in gen_inputs.items():
            actual_v_ndim = v.ndim + (1 if v.dtype == torch.long and
                                      not isinstance(self.gen_bn.name_to_v[k], MultiCategorical) else 0)
            for _ in range(max_n_dims-actual_v_ndim):
                expand_arg = [n_iw]+list(gen_inputs[k].shape)
                gen_inputs[k] = gen_inputs[k].unsqueeze(0).expand(expand_arg)
        return gen_inputs

    def get_perplexity(self, iterator, format_func):
        with torch.no_grad():
            nneg_log_perplexity_lb = 0
            cneg_log_perplexity_lb = 0
            ntotal_samples = 0
            ctotal_samples = 0
            prev_states = None
            force_iw = ['w']
            niwlbo = IWLBo(self.noise_model, 1)
            ciwlbo = IWLBo(self.clean_model, 1)

            ciwlbo.gen_lvs.pop('zcom', None)
            ciwlbo.infer_lvs.pop('zcom', None)

            for i, batch in enumerate(tqdm(iterator, desc="Getting Noisy/Clean Perplexities")):
                formatted_batch = format_func(batch)
                prev_states = self(formatted_batch, prev_states=prev_states, force_iw=force_iw)
                if not self.h_params.contiguous_lm:
                    prev_states = None
                # Clean calculation
                celbo = - ciwlbo.get_loss(actual=True)
                ctotal_samples_i = torch.sum(formatted_batch['clean']['c'] != self.h_params.c_ignore_index)
                cneg_log_perplexity_lb += celbo * ctotal_samples_i

                ctotal_samples += ctotal_samples_i
                # Noise calculation
                nelbo = - niwlbo.get_loss(actual=True)
                ntotal_samples_i = torch.sum(formatted_batch['noise']['c'] != self.h_params.c_ignore_index)
                nneg_log_perplexity_lb += nelbo * ntotal_samples_i

                ntotal_samples += ntotal_samples_i

            cneg_log_perplexity_lb /= ctotal_samples
            cperplexity_ub = torch.exp(- cneg_log_perplexity_lb)

            nneg_log_perplexity_lb /= ntotal_samples
            nperplexity_ub = torch.exp(- nneg_log_perplexity_lb)

            self.writer.add_scalar('test/CleanPerplexityUB', cperplexity_ub, self.step)
            self.writer.add_scalar('test/NoisePerplexityUB', nperplexity_ub, self.step)
            return nperplexity_ub, cperplexity_ub

    def save(self):
        root = ''
        for subfolder in self.h_params.save_path.split(os.sep)[:-1]:
            root = os.path.join(root, subfolder)
            if not os.path.exists(root):
                os.mkdir(root)
        torch.save({'model_checkpoint': self.state_dict(), 'step': self.step}, self.h_params.save_path)
        print("Model {} saved !".format(self.h_params.test_name))

    def load(self):
        if os.path.exists(self.h_params.save_path):
            checkpoint = torch.load(self.h_params.save_path)
            model_checkpoint, self.step = checkpoint['model_checkpoint'], checkpoint['step']
            self.load_state_dict(model_checkpoint)
            print("Loaded model at step", self.step)
        else:
            print("Save file doesn't exist, the model will be trained from scratch.")

    def reduce_lr(self, factor):
        for param_group in self.optimizer.param_groups:
            param_group['lr'] /= factor

    def eval(self):
        for v in self.clean_model.infer_bn.variables | self.clean_model.gen_bn.variables:
            if v.name != self.clean_model.generated_v.name and (isinstance(v, MultiCategorical)
                                                                or isinstance(v, Categorical)):
                v.switch_to_non_relaxed()

        for v in self.noise_model.infer_bn.variables | self.noise_model.gen_bn.variables:
            if v.name != self.noise_model.generated_v.name and (isinstance(v, MultiCategorical)
                                                                or isinstance(v, Categorical)):
                v.switch_to_non_relaxed()
        super(BaseTrainingHandler, self).eval()

    def train(self, mode=True):
        for v in self.clean_model.infer_bn.variables | self.clean_model.gen_bn.variables:
            if v.name != self.clean_model.generated_v.name and (isinstance(v, MultiCategorical)
                                                                or isinstance(v, Categorical)):
                v.switch_to_relaxed()
        for v in self.noise_model.infer_bn.variables | self.noise_model.gen_bn.variables:
            if v.name != self.noise_model.generated_v.name and (isinstance(v, MultiCategorical)
                                                                or isinstance(v, Categorical)):
                v.switch_to_relaxed()
        super(BaseTrainingHandler, self).train(mode=mode)


class UnsupervisedTrainingHandler(BaseTrainingHandler):

    def __init__(self, vocab_index, char_index, h_params, autoload=True, wvs=None):
        super(UnsupervisedTrainingHandler, self).__init__(vocab_index, char_index, h_params, autoload, wvs)
        # preventing KL optimization on zcom
        self.losses['unsup_noise'].gen_lvs.pop('zcom', None)
        self.losses['unsup_noise'].infer_lvs.pop('zcom', None)
        self.losses = {**{loss_name:loss for loss_name, loss in self.losses.items() if loss_name.endswith('noise')}}

    def opt_step(self, samples):
        if (self.step % self.h_params.grad_accumulation_steps) == 0:
            # Reinitializing gradients if accumulation is over
            self.optimizer.zero_grad()
        #                          ----------- Unsupervised Forward/Backward ----------------
        # Forward pass
        self.noise_model(samples['noise'], plant_gen_posteriors={'yorig':
                                                                     {'logits':
                                                                          samples['noise']['yorig'].float().log()}})

        # Loss computation and backward pass
        loss_vals = [loss.get_loss() * loss.w for name, loss in self.losses.items() if name.endswith('noise')]
        sum(loss_vals).backward()
        if not self.h_params.contiguous_lm:
            self.infer_last_states, self.gen_last_states = None, None

        if (self.step % self.h_params.grad_accumulation_steps) == (self.h_params.grad_accumulation_steps - 1):
            # Applying gradients and gradient clipping if accumulation is over
            torch.nn.utils.clip_grad_norm_(self.parameters(), self.h_params.grad_clip)
            self.optimizer.step()
        self.step += 1
        self.noise_model.step += 1

        self._dump_train_viz()
        total_loss = sum(loss_vals)

        return total_loss

    def forward(self, samples, eval=False, prev_states=None, force_iw=None, substitute_gen_vals=None):
        prev_states = prev_states or {'noise': None}
        #                          ----------- Forward pass ----------------

        noise_prev_states = self.noise_model(samples['noise'], eval=eval, prev_states=prev_states['noise'],
                                             force_iw=force_iw, substitute_gen_vals=None,
                                             plant_gen_posteriors={'yorig':
                                                                       {'logits':
                                                                            samples['noise']['yorig'].float().log()}})

        # Loss computation
        [loss.get_loss() * loss.w for name, loss in self.losses.items() if name.endswith('noise')]
        return {'noise': noise_prev_states} if self.h_params.contiguous_lm else {'noise': None}

    def data_specific_metrics(self):
        # this is supposed to output a list of (summary type, summary name, summary data) triplets
        with torch.no_grad():
            summary_triplets = [
                ('text', '/noise_ground_truth',
                 self.decode_to_text(self.noise_model.gen_bn.variables_star[self.noise_model.generated_v])),
                ('text', '/noise_reconstructions',
                 self.decode_to_text(self.noise_model.generated_v.post_params['logits']))
            ]
        return summary_triplets

    def get_perplexity(self, iterator, format_func):
        with torch.no_grad():
            nneg_log_perplexity_lb = 0
            ntotal_samples = 0
            prev_states = None
            force_iw = ['w']
            niwlbo = IWLBo(self.noise_model, 1)
            niwlbo.gen_lvs.pop('zcom', None)
            niwlbo.infer_lvs.pop('zcom', None)

            for i, batch in enumerate(tqdm(iterator, desc="Getting Noisy Perplexity")):
                formatted_batch = format_func(batch)
                prev_states = self(formatted_batch, prev_states=prev_states, force_iw=force_iw)
                if not self.h_params.contiguous_lm:
                    prev_states = None

                # Noise calculation
                nelbo = - niwlbo.get_loss(actual=True)
                ntotal_samples_i = torch.sum(formatted_batch['noise']['c']!= self.h_params.c_ignore_index)
                nneg_log_perplexity_lb += nelbo * ntotal_samples_i

                ntotal_samples += ntotal_samples_i

            nneg_log_perplexity_lb /= ntotal_samples
            nperplexity_ub = torch.exp(- nneg_log_perplexity_lb)

            self.writer.add_scalar('test/NoisePerplexityUB', nperplexity_ub, self.step)
            return nperplexity_ub, None


class DistantlySupervisedTrainingHandler(BaseTrainingHandler):

    def __init__(self, vocab_index, char_index, h_params, autoload=True, wvs=None):
        super(DistantlySupervisedTrainingHandler, self).__init__(vocab_index, char_index, h_params, autoload, wvs)
        # preventing KL optimization on zcom for the clean examples
        self.losses['unsup_clean'].gen_lvs.pop('zcom', None)
        self.losses['unsup_clean'].infer_lvs.pop('zcom', None)

    def opt_step(self, samples):
        if (self.step % self.h_params.grad_accumulation_steps) == 0:
            # Reinitializing gradients if accumulation is over
            self.optimizer.zero_grad()
        #                          ----------- Unsupervised Forward/Backward ----------------
        self.clean_model(samples['clean'],
                         plant_gen_posteriors={'yorig':{'logits': samples['clean']['yorig'].float().log()}})
        # Putting the prior on noisy zcom to the right value for its KL calculation
        self.noise_model.gen_bn.name_to_v['zcom'].posterior_params = {
            'loc': self.clean_model.infer_bn.name_to_v['zcom'].post_samples.mean(-3).unsqueeze(-3),
            'scale': self.clean_model.infer_bn.name_to_v['zcom'].post_samples.std(-3).unsqueeze(-3)}
        self.noise_model(samples['noise'],
                         plant_gen_posteriors={'yorig':{'logits': samples['noise']['yorig'].float().log()},
                                               'zcom':{
            'loc': self.clean_model.infer_bn.name_to_v['zcom'].post_samples.mean(-3).unsqueeze(-3),
            'scale': self.clean_model.infer_bn.name_to_v['zcom'].post_samples.std(-3).unsqueeze(-3)}})

        # Loss computation and backward pass
        loss_vals = [loss.get_loss() * loss.w for name, loss in self.losses.items()]
        sum(loss_vals).backward()
        if not self.h_params.contiguous_lm:
            self.infer_last_states, self.gen_last_states = None, None

        if (self.step % self.h_params.grad_accumulation_steps) == (self.h_params.grad_accumulation_steps - 1):
            # Applying gradients and gradient clipping if accumulation is over
            torch.nn.utils.clip_grad_norm_(self.parameters(), self.h_params.grad_clip)
            self.optimizer.step()
        self.step += 1
        self.noise_model.step += 1
        self.clean_model.step += 1

        self._dump_train_viz()
        total_loss = sum(loss_vals)

        return total_loss

    def forward(self, samples, eval=False, prev_states=None, force_iw=None, substitute_gen_vals=None):
        prev_states = prev_states or {'noise': None, 'clean': None}

        #                          ----------- Forward pass ----------------
        clean_prev_states = self.clean_model(samples['clean'],
                                             plant_gen_posteriors={
                                                 'yorig': {'logits': samples['clean']['yorig'].float().log()}},
                                             eval=eval, prev_states=prev_states['clean'], force_iw=force_iw)
        # Putting the prior on noisy zcom to the right value for its KL calculation
        self.noise_model.gen_bn.name_to_v['zcom'].posterior_params = {
            'loc': self.clean_model.infer_bn.name_to_v['zcom'].post_samples.mean(-3).unsqueeze(-3),
            'scale': self.clean_model.infer_bn.name_to_v['zcom'].post_samples.std(-3).unsqueeze(-3)}
        noise_prev_states = self.noise_model(samples['noise'],
                                             plant_gen_posteriors={
                                                 'yorig': {'logits': samples['noise']['yorig'].float().log()},
                                                 'zcom': {
                                                     'loc': self.clean_model.infer_bn.name_to_v[
                                                         'zcom'].post_samples.mean(-3).unsqueeze(-3),
                                                     'scale': self.clean_model.infer_bn.name_to_v[
                                                         'zcom'].post_samples.std(-3).unsqueeze(-3)}}, eval=eval,
                                             prev_states=prev_states['noise'], force_iw=force_iw)

        # Loss computation
        [loss.get_loss() * loss.w for name, loss in self.losses.items() if name.endswith('noise')]
        return {'noise': noise_prev_states, 'clean': clean_prev_states} if self.h_params.contiguous_lm else\
               {'noise': None, 'clean': None}


class SupervisedTrainingHandler(BaseTrainingHandler):

    def __init__(self, vocab_index, char_index, h_params, autoload=True, wvs=None):
        super(SupervisedTrainingHandler, self).__init__(vocab_index, char_index, h_params, autoload, wvs)
        # preventing KL optimization on zcom for the clean examples
        self.losses['unsup_clean'].gen_lvs.pop('zcom', None)
        self.losses['unsup_clean'].infer_lvs.pop('zcom', None)

        # Building noisy_to_clean loss
        self.n_to_c_loss = ELBo(self.clean_model, 1)
        self.n_to_c_loss.infer_lvs['zcom'] = self.losses['unsup_noise'].infer_lvs['zcom']

    def opt_step(self, samples):
        if (self.step % self.h_params.grad_accumulation_steps) == 0:
            # Reinitializing gradients if accumulation is over
            self.optimizer.zero_grad()
        #                          ----------- Unsupervised Forward/Backward ----------------
        self.clean_model(samples['clean'], plant_gen_posteriors={
                                                 'yorig': {'logits': samples['clean']['yorig'].float().log()}},)
        # Putting the prior on noisy zcom to the right value for its KL calculation
        self.noise_model(samples['noise'],  plant_gen_posteriors={'yorig':
                                                                       {'logits':
                                                                            samples['noise']['yorig'].float().log()},
                                                                   'zcom':{
            'loc': self.clean_model.infer_bn.name_to_v['zcom'].post_samples.mean(-3).unsqueeze(-3),
            'scale': self.clean_model.infer_bn.name_to_v['zcom'].post_samples.std(-3).unsqueeze(-3)}})

        # Loss computation
        loss_vals = [loss.get_loss() * loss.w for name, loss in self.losses.items()]

        # Noise to clean forward pass
        substitute_zcom_val = self.noise_model.infer_bn.variables_hat[self.noise_model.infer_bn.name_to_v['zcom']]
        self.clean_model(samples['clean'], plant_gen_posteriors={
                                                 'yorig': {'logits': samples['clean']['yorig'].float().log()}},
                         only_gen=True, substitute_gen_vals={'zcom': substitute_zcom_val})
        loss_vals += [self.n_to_c_loss.get_loss() * self.n_to_c_loss.w]

        # Backward pass
        sum(loss_vals).backward()
        if not self.h_params.contiguous_lm:
            self.infer_last_states, self.gen_last_states = None, None

        if (self.step % self.h_params.grad_accumulation_steps) == (self.h_params.grad_accumulation_steps - 1):
            # Applying gradients and gradient clipping if accumulation is over
            torch.nn.utils.clip_grad_norm_(self.parameters(), self.h_params.grad_clip)
            self.optimizer.step()
        self.step += 1
        self.noise_model.step += 1
        self.clean_model.step += 1

        self._dump_train_viz()
        total_loss = sum(loss_vals)

        return total_loss

    def forward(self, samples, eval=False, prev_states=None, force_iw=None, substitute_gen_vals=None):
        prev_states = prev_states or {'noise': None, 'clean': None}

        #                          ----------- Forward pass ----------------
        clean_prev_states = self.clean_model(samples['clean'],
                                             plant_gen_posteriors={
                                                 'yorig': {'logits': samples['clean']['yorig'].float().log()}},
                                             eval=eval, prev_states=prev_states['clean'], force_iw=force_iw)
        # Putting the prior on noisy zcom to the right value for its KL calculation
        noise_prev_states = self.noise_model(samples['noise'],
                                             plant_gen_posteriors={'yorig':
                                                                       {'logits':
                                                                            samples['noise']['yorig'].float().log()},
                                                                   'zcom':{
            'loc': self.clean_model.infer_bn.name_to_v['zcom'].post_samples.mean(-3).unsqueeze(-3),
            'scale': self.clean_model.infer_bn.name_to_v['zcom'].post_samples.std(-3).unsqueeze(-3)}},
                                             eval=eval, prev_states=prev_states['noise'], force_iw=force_iw)

        # Loss computation
        [loss.get_loss() * loss.w for name, loss in self.losses.items() if name.endswith('noise')]
        # Noise to clean forward pass
        substitute_zcom_val = self.noise_model.infer_bn.variables_hat[self.noise_model.infer_bn.name_to_v['zcom']]
        self.clean_model(samples['clean'], plant_gen_posteriors={
                                                 'yorig': {'logits': samples['clean']['yorig'].float().log()}},
                         only_gen=True, eval=eval, prev_states=prev_states['clean'], force_iw=force_iw,
                         substitute_gen_vals={'zcom': substitute_zcom_val})
        self.n_to_c_loss.get_loss()
        return {'noise': noise_prev_states, 'clean': clean_prev_states} if self.h_params.contiguous_lm else\
               {'noise': None, 'clean': None}

    def _dump_train_viz(self):
        super(SupervisedTrainingHandler, self)._dump_train_viz()
        for name, metric in self.n_to_c_loss.metrics().items():
            self.writer.add_scalar('train' + name + '[n2c]', metric, self.step)

    def dump_test_viz(self, complete=False):
        super(SupervisedTrainingHandler, self).dump_test_viz(complete=complete)
        for name, metric in self.n_to_c_loss.metrics().items():
            self.writer.add_scalar('train' + name + '[n2c]', metric, self.step)


class SemiSupervisedTrainingHandler(BaseTrainingHandler):

    def __init__(self, vocab_index, char_index, h_params, autoload=True, wvs=None):
        super(SemiSupervisedTrainingHandler, self).__init__(vocab_index, char_index, h_params, autoload, wvs)
        # preventing KL optimization on zcom for the clean examples
        self.losses['unsup_clean'].gen_lvs.pop('zcom', None)
        self.losses['unsup_clean'].infer_lvs.pop('zcom', None)

        # Building noisy_to_clean loss
        self.n_to_c_loss = ELBo(self.clean_model, 1)
        self.n_to_c_loss.infer_lvs['zcom'] = self.losses['unsup_noise'].infer_lvs['zcom']

        # Distantly Supervised/Supervised switch
        self.is_distant = False

    def opt_step(self, samples):
        if (self.step % self.h_params.grad_accumulation_steps) == 0:
            # Reinitializing gradients if accumulation is over
            self.optimizer.zero_grad()
        #                          ----------- Unsupervised Forward/Backward ----------------
        self.clean_model(samples['clean'], plant_gen_posteriors={
                                                 'yorig': {'logits': samples['clean']['yorig'].float().log()}})
        # Putting the prior on noisy zcom to the right value for its KL calculation
        if self.is_distant:
            zcom_post = {
                'loc': self.clean_model.infer_bn.name_to_v['zcom'].post_samples.mean(-3).unsqueeze(-3),
                'scale': self.clean_model.infer_bn.name_to_v['zcom'].post_samples.std(-3).unsqueeze(-3)}
        else:
            zcom_post = {
                'loc': self.clean_model.infer_bn.name_to_v['zcom'].post_samples,
                'scale': self.clean_model.infer_bn.name_to_v['zcom'].post_samples}
        self.noise_model(samples['noise'], plant_gen_posteriors={'yorig':{'logits': samples['noise']['yorig'].float().log()},
                                                                   'zcom': zcom_post})

        # Loss computation
        loss_vals = [loss.get_loss() * loss.w for name, loss in self.losses.items()]
        if not self.is_distant:
            # Noise to clean forward pass
            substitute_zcom_val = self.noise_model.infer_bn.variables_hat[self.noise_model.infer_bn.name_to_v['zcom']]
            self.clean_model(samples['clean'], plant_gen_posteriors={
                                                 'yorig': {'logits': samples['clean']['yorig'].float().log()}},
                             only_gen=True, substitute_gen_vals={'zcom': substitute_zcom_val})
            loss_vals += [self.n_to_c_loss.get_loss() * self.n_to_c_loss.w]

        # Backward pass
        sum(loss_vals).backward()
        if not self.h_params.contiguous_lm:
            self.infer_last_states, self.gen_last_states = None, None

        if (self.step % self.h_params.grad_accumulation_steps) == (self.h_params.grad_accumulation_steps - 1):
            # Applying gradients and gradient clipping if accumulation is over
            torch.nn.utils.clip_grad_norm_(self.parameters(), self.h_params.grad_clip)
            self.optimizer.step()
        self.step += 1
        self.noise_model.step += 1
        self.clean_model.step += 1

        self._dump_train_viz()
        total_loss = sum(loss_vals)

        return total_loss

    def forward(self, samples, eval=False, prev_states=None, force_iw=None, substitute_gen_vals=None):
        prev_states = prev_states or {'noise': None, 'clean': None}

        #                          ----------- Forward pass ----------------
        clean_prev_states = self.clean_model(samples['clean'],
                                             plant_gen_posteriors={
                                                 'yorig': {'logits': samples['clean']['yorig'].float().log()}},
                                             eval=eval, prev_states=prev_states['clean'], force_iw=force_iw)
        # Putting the prior on noisy zcom to the right value for its KL calculation
        noise_prev_states = self.noise_model(samples['noise'],
                                             plant_gen_posteriors={'yorig':{'logits': samples['noise']['yorig'].float().log()},
                                                                   'zcom':{
            'loc': self.clean_model.infer_bn.name_to_v['zcom'].post_samples.mean(-3).unsqueeze(-3),
            'scale': self.clean_model.infer_bn.name_to_v['zcom'].post_samples.std(-3).unsqueeze(-3)}},
                                             eval=eval, prev_states=prev_states['noise'], force_iw=force_iw)

        # Loss computation
        [loss.get_loss() * loss.w for name, loss in self.losses.items() if name.endswith('noise')]
        # Noise to clean forward pass
        substitute_zcom_val = self.noise_model.infer_bn.variables_hat[self.noise_model.infer_bn.name_to_v['zcom']]
        self.clean_model(samples['clean'], plant_gen_posteriors={
                                                 'yorig': {'logits': samples['clean']['yorig'].float().log()}},
                         only_gen=True, eval=eval, prev_states=prev_states['clean'], force_iw=force_iw,
                         substitute_gen_vals={'zcom': substitute_zcom_val})
        self.n_to_c_loss.get_loss()
        return {'noise': noise_prev_states, 'clean': clean_prev_states} if self.h_params.contiguous_lm else\
               {'noise': None, 'clean': None}

    def _dump_train_viz(self):
        super(SemiSupervisedTrainingHandler, self)._dump_train_viz()
        if not self.is_distant:
            for name, metric in self.n_to_c_loss.metrics().items():
                self.writer.add_scalar('train' + name + '[n2c]', metric, self.step)

    def dump_test_viz(self, complete=False):
        super(SemiSupervisedTrainingHandler, self).dump_test_viz(complete=complete)
        if not self.is_distant:
            for name, metric in self.n_to_c_loss.metrics().items():
                self.writer.add_scalar('train' + name + '[n2c]', metric, self.step)



