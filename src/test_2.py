'''
Hypothesis 2: fine-tuning one of the models using aggregated label integrated training (ALI training, alint training,
agg-int training, ITwAL) boosts performance.


-- nans in dev score?
-- names and indexes of annotators are not in the right order. Are we blanking out the wrong ones? Or do names just need sorting?

'''
import os
import shutil

import numpy as np
import json

#from baselines.dawid_and_skene import ibccvb
from base_models import run_base_models
from bsc import bsc
from helpers import evaluate, Dataset, get_anno_matrix, get_anno_names, get_root_dir, append_training_labels
from lample_lstm_tagger.lstm_wrapper import data_to_lstm_format
from seq_taggers import embpath

reload = True
rerun_aggregators = True
verbose = False

datadir = os.path.join(get_root_dir(), 'data/famulus_TEd')

base_models = ['bilstm-crf', 'crf'] # , 'flair-pos', 'flair-ner']

#iterate through the types of span we want to predict
for classid in [0, 1, 2, 3]:

    basemodels_str = '--'.join(base_models)

    resdir = os.path.join(get_root_dir(), 'output/famulus_TEd_task2_type%i_basemodels%s_crfprobs' % (classid, basemodels_str) )
    if not os.path.exists(resdir):
        os.mkdir(resdir)

    predfile = os.path.join(resdir, 'preds.json')
    trpredfile = os.path.join(resdir, 'trpreds.json')
    resfile = os.path.join(resdir, 'res.json')

    if reload and not rerun_aggregators and os.path.exists(predfile):
        with open(predfile, 'r') as fh:
            preds = json.load(fh)

        with open(trpredfile, 'r') as fh:
            trpreds = json.load(fh)

        with open(resfile, 'r') as fh:
            res = json.load(fh)
    else:
        preds = {}
        trpreds = {}
        res = {}

    for base_model_str in base_models:

        for classid2 in [0, 1, 2, 3]:
            if classid2 != classid:
                continue

            dataset2 = Dataset(datadir, classid2)
            if classid2 == classid:
                dataset = dataset2

            basepreds, basetrpreds, baseres = run_base_models(dataset2, classid2, base_model_str, reload)
            for key in basepreds:

                if key == 'a' or key == 'MV' or key == 'baseline_every' or 'ibcc' in key or 'bsc-seq' in key:
                    continue # 'a' is the in-domain performance. We don't use the in-domain model as part of an ensemble.
                    # The others are crap that shouldn't be in there.

                if len(basepreds[key]) == 0:
                    continue # skip any entries that don't really have predictions. Why are they there?

                ntest_domains = len(basepreds[key])

                if verbose:
                    print('Processing model type %s, base labeller %s we found %i sets of test results' %
                      (base_model_str, key, ntest_domains))

                new_key = base_model_str + '_' + str(classid2) + '__' + key

                preds[new_key] = basepreds[key]
                trpreds[new_key] = basetrpreds[key]

    alpha0_factor = 0.1
    alpha0_diags = 10
    nu0_factor = 0.1

    max_iter = 30

    allgold = []
    alldocstart = []

    for didx, tedomain in enumerate(dataset.domains):
        allgold.append(dataset.tegold[tedomain])
        alldocstart.append(dataset.tedocstart[tedomain])

    if rerun_aggregators or 'agg_bsc-seq' not in res or not len(res['agg_bsc-seq']):

        preds['agg_bsc-seq'] = []
        res['agg_bsc-seq'] = []

        for didx, tedomain in enumerate(dataset.domains):

            # First, put the base labellers into a table.
            annos, uniform_priors = get_anno_matrix(classid, preds, didx, include_all=False)
            docstart = dataset.tedocstart[tedomain]
            text = dataset.tetext[tedomain]
            annos, docstart, text, trlabels = append_training_labels(annos, basemodels_str, dataset, classid, didx, tedomain, trpreds, 20)


            K = annos.shape[1] # number of annotators

            # Run BSC-seq to determine the best base model.
            bsc_model = bsc.BSC(L=3, K=K, max_iter=max_iter, before_doc_idx=1,
                        alpha0_diags=alpha0_diags, alpha0_factor=alpha0_factor, beta0_factor=nu0_factor,
                        worker_model='seq', tagging_scheme='IOB2', data_model=[], transition_model='HMM',
                        no_words=True, eps=1e-2)
            bsc_model.verbose = False
            bsc_model.max_internal_iters = max_iter
            # why does Beta put a lot of weight on going from 2 to 0? Too much trust in 1 labels?
            probs, agg, pseq = bsc_model.run(annos, docstart, text,
                                 converge_workers_first=False, uniform_priors=uniform_priors, gold_labels=trlabels)

            agg = agg[:len(dataset.tetext[tedomain])]
            preds['agg_bsc-seq'].append(agg.flatten().tolist())

            res_s = evaluate(agg, dataset.tegold[tedomain], dataset.tedocstart[tedomain], f1type='all')
            print('   Spantype %i: F1 score=%s for BSC-seq, tested on %s' % (classid, str(np.around(res_s, 2)), tedomain))

            # now we compute the base model informativeness
            competence = bsc_model.annotator_accuracy()
            competence[np.all(annos == -1, axis=0)] = 0  # we don't choose the model that has no labels for this domain
            # print('Accuracy of base models:')
            # print(competence)

            competence = bsc_model.informativeness()
            competence[np.all(annos == -1, axis=0)] = -np.inf  # we don't choose the model that has no labels for this domain
            # print('Informativeness of base models:')
            # print(competence)

            names = get_anno_names(classid, preds, include_all=False)
            print('Names of the annotators: %s' % str(names))
            # bilstms == we only want to tune these right now
            tuneables = [name.split('_')[0] == 'bilstm-crf' for name in names]

            if np.any(tuneables):
                competence[np.invert(tuneables)] = -np.inf # exclude the models that are not tuneable
                print('Informativeness of tuneable base models:')
                print(competence)

                best_base_idx = np.argmax(competence)
                print(best_base_idx)

                best_base = names[best_base_idx]
                print('Chosen for pre-training: %s' % best_base)

                # copy the model we want to fine-tune
                new_dir = os.path.join(get_root_dir(), 'output/tmp_spantype%i_tunedfor%s_basemodels%s/%s' %
                                             (classid, tedomain, basemodels_str, best_base.split('__')[-1]))
                orig_dir = os.path.join(get_root_dir(), 'output/tmp_spantype%i/%s' %
                                              (classid, best_base.split('__')[-1]))

                if os.path.exists(new_dir):
                    shutil.rmtree(new_dir)
                    print('removed %s' % new_dir)
                shutil.copytree(orig_dir, new_dir)
                print('copied %s to %s' % (orig_dir, new_dir))

                # fine tuning will use a different setting for the BILSTM CRF
                model_dirs = os.listdir(new_dir)
                for model_dir in model_dirs:
                    new_model_dir = model_dir.replace('crf_probs=False', 'crf_probs=True')
                    shutil.copytree(os.path.join(new_dir, model_dir), os.path.join(new_dir, new_model_dir))

                static_annotators = np.arange(annos.shape[1])
                static_annotators = static_annotators[static_annotators != best_base_idx]
                print('Debugging: for now we are not removing the original labels.')

                reload_lstm = True
            else:
                new_dir = os.path.join(get_root_dir(), 'output/tmp_spantype%i_tunedfor%s_basemodels%s/new_target_model' %
                                             (classid, tedomain, basemodels_str))
                reload_lstm = False

            annos_fixed = annos  #[:, static_annotators]
            K = annos_fixed.shape[1]

            # create a new BSC instance with the LSTM data model and pass in the model directory.
            bsc_model = bsc.BSC(L=3, K=K, max_iter=max_iter, before_doc_idx=1,
                        alpha0_diags=alpha0_diags, alpha0_factor=alpha0_factor, beta0_factor=nu0_factor,
                        worker_model='seq', tagging_scheme='IOB2', data_model=['LSTM'], transition_model='HMM',
                        no_words=True, model_dir=new_dir, reload_lstm=reload_lstm, embeddings_file=embpath, eps=1e-2)
            bsc_model.verbose = False
            bsc_model.max_internal_iters = 20

            # C_data_initial = [np.zeros((annos.shape[0], 3))]
            # for tag in range(3):
            #     C_data_initial[0][:, tag] = (annos[:, best_base_idx] == tag).astype(float)

            Nde = len(dataset.degold[tedomain])
            dev_sentences, _, _ = data_to_lstm_format(Nde, dataset.detext[tedomain],
                                                      dataset.dedocstart[tedomain],
                                                      dataset.degold[tedomain].flatten(), 3)

            # why does Beta put a lot of weight on going from 2 to 0? Too much trust in 1 labels?
            probs, agg, pseq = bsc_model.run(annos_fixed, docstart, text,
                             converge_workers_first=True, uniform_priors=uniform_priors, #C_data_initial=C_data_initial,
                             crf_probs=True, gold_labels=trlabels) # dev_sentences=dev_sentences,  shouldn't have this as it's not realistic for our scenario
            agg = agg[:len(dataset.tetext[tedomain])]

            preds['agg_bsc-seq-VCS'].append(agg.flatten().tolist())

            aggprob = np.argmax(probs, axis=1)

            res_s = evaluate(agg, dataset.tegold[tedomain], dataset.tedocstart[tedomain], f1type='all')
            res['agg_bsc-seq'].append(res_s)
            print('   Spantype %i: F1 score=%s for BSC-seq+CVS, tested on %s' % (classid, str(np.around(res_s, 2)), tedomain))

        # save all the new results
        with open(predfile, 'w') as fh:
            json.dump(preds, fh)

        with open(resfile, 'w') as fh:
            json.dump(res, fh)

    else:
        for didx, tedomain in enumerate(dataset.domains):
            print('   Spantype %i: F1 score=%f for BSC-seq, tested on %s' % (classid, res['agg_bsc-seq'][didx], tedomain))

    cross_f1 = evaluate(np.concatenate(preds['agg_bsc-seq']),
                        np.concatenate(allgold),
                        np.concatenate(alldocstart),
                        f1type='all')
    print('*** Spantype %i, F1 score=%s for BSC-seq  (micro average over domains) ' % (classid, str(cross_f1)) )
    print('*** Spantype %i, F1 score=%s for BSC-seq  (macro average over domains) ' % (classid, str(np.mean(res['agg_bsc-seq'], axis=0))) )
