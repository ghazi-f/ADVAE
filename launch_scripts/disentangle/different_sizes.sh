#!/usr/bin/env bash

# ================= EXP GROUP 1: Ours-4 ==============================
# Experiments on the SNLI Dataset
#for BETA in 0.4 0.3
#do
#for ITER in 1 2 3 4 5
#do
#python disentangle_train.py --test_name "Disentanglement/SNLIRegular_beta$BETA.$ITER" --kl_beta $BETA --n_latents 4 --data nli --n_epochs 20 --graph IndepInfer
#done
#done
#
## Experiments on the Yelp Dataset
#for BETA in 0.3 0.5 #0.4
#do
#for ITER in 1 2 3 4 5
#do
#python disentangle_train.py --test_name "Disentanglement/YelpRegular_beta$BETA.$ITER" --kl_beta $BETA --n_latents 4 --data yelp --n_epochs 20 --graph IndepInfer
#done
#done


# ================= EXP GROUP 2: Sequence VAE SNLI ==============================
# Experiments on the SNLI Dataset
#for BETA in 0.5 #0.4 0.3
#do
#for ITER in 1 2 3 4 5
#do
#python disentangle_train.py --test_name "Disentanglement/SNLIVanillaVAEmzsh1_beta$BETA.$ITER" --kl_beta $BETA --data nli --n_epochs 20 --graph Vanilla --z_size 32 --encoder_h 256 --decoder_h 256
#python disentangle_train.py --test_name "Disentanglement/SNLIVanillaVAEszsh1_beta$BETA.$ITER" --kl_beta $BETA --data nli --n_epochs 20 --graph Vanilla --z_size 16 --encoder_h 256 --decoder_h 256
#python disentangle_train.py --test_name "Disentanglement/SNLIVanillaVAEmzmh1_beta$BETA.$ITER" --kl_beta $BETA --data nli --n_epochs 20 --graph Vanilla --z_size 32 --encoder_h 512 --decoder_h 512
#python disentangle_train.py --test_name "Disentanglement/SNLIVanillaVAEszmh1_beta$BETA.$ITER" --kl_beta $BETA --data nli --n_epochs 20 --graph Vanilla --z_size 16 --encoder_h 512 --decoder_h 512
#done
#done
#
## ================= EXP GROUP 3: Sequence VAE Yelp Dataset ==============================
## Experiments on the Yelp Dataset
#for BETA in 0.3 0.5 #0.4
#do
#for ITER in 1 2 3 4 5
#do
#python disentangle_train.py --test_name "Disentanglement/YelpVanillaVAEmzsh1_beta$BETA.$ITER" --kl_beta $BETA --data yelp --n_epochs 20 --graph Vanilla --z_size 32 --encoder_h 256 --decoder_h 256
#python disentangle_train.py --test_name "Disentanglement/YelpVanillaVAEszsh1_beta$BETA.$ITER" --kl_beta $BETA --data yelp --n_epochs 20 --graph Vanilla --z_size 16 --encoder_h 256 --decoder_h 256
#python disentangle_train.py --test_name "Disentanglement/YelpVanillaVAEmzmh1_beta$BETA.$ITER" --kl_beta $BETA --data yelp --n_epochs 20 --graph Vanilla --z_size 32 --encoder_h 512 --decoder_h 512
#python disentangle_train.py --test_name "Disentanglement/YelpVanillaVAEszmh1_beta$BETA.$ITER" --kl_beta $BETA --data yelp --n_epochs 20 --graph Vanilla --z_size 16 --encoder_h 512 --decoder_h 512
#done
#done
#
## ================= EXP GROUP 4: Ours-8 and Hierachical models ==============================
## Experiments on the SNLI Dataset
#for BETA in   0.4 0.3 #0.5
#do
#for ITER in 1 2 3 4 5
#do
#python disentangle_train.py --test_name "Disentanglement/SNLIWide_beta$BETA.$ITER" --kl_beta $BETA --n_latents 8 --data nli --n_epochs 20 --graph IndepInfer
#python disentangle_train.py --test_name "Disentanglement/SNLIDeep3_beta$BETA.$ITER" --kl_beta $BETA --n_latents 4 4 --data nli --n_epochs 20 --graph IndepInfer
#python disentangle_train.py --test_name "Disentanglement/SNLIDeep2_beta$BETA.$ITER" --kl_beta $BETA --n_latents 4 4 4 --data nli --n_epochs 20 --graph IndepInfer
#done
#done
## Experiments on the Yelp Dataset
#for BETA in 0.4 0.3 0.5
#do
#for ITER in 1 2 3 4 5
#do
#python disentangle_train.py --test_name "Disentanglement/YelpWide_beta$BETA.$ITER" --kl_beta $BETA --n_latents 8 --data yelp --n_epochs 20 --graph IndepInfer
#python disentangle_train.py --test_name "Disentanglement/YelpDeep3_beta$BETA.$ITER" --kl_beta $BETA --n_latents 4 4 --data yelp --n_epochs 20 --graph IndepInfer
#python disentangle_train.py --test_name "Disentanglement/YelpDeep2_beta$BETA.$ITER" --kl_beta $BETA --n_latents 4 4 4 --data yelp --n_epochs 20 --graph IndepInfer
#done
#done
