#!/bin/sh
# Submit boosting jobs to SGE + parallel environment - OpenMP support
# Author: Michal Hradis 
# email: ihradi@fit.vutbr.cz

QUE_LIST=all.q
TASK_ID=OLLAMA
RESOURCES=ram_free=6G,mem_free=6G,gpu=1,gpu_ram=22G

qsub -t 1-20000 -N $TASK_ID -l $RESOURCES -q $QUE_LIST ./SGE-jobs.sh