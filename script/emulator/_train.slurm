#!/bin/bash
#SBATCH -J NMF_ZH_4
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
#SBATCH --mem=56G
#SBATCH --time=24:00:00
#SBATCH --export=ALL
#SBATCH -o NMF_ZH_4.o
#SBATCH --mail-type=all
#SBATCH --mail-user=jiaxuanl@princeton.edu

now=$(date +"%T")
echo "start time ... $now"

module purge
. /home/jiaxuanl/Research/popsed/script/setup_env.sh

python train_emulator_ZH.py --name=NMF_ZH --i_bin=4 --file_low=0 --file_high=10 --batch_size=512 --rounds=6
now=$(date +"%T")
echo "end time ... $now"
