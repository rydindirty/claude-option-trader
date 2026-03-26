#!/bin/bash
# Navigate to project and run the pipeline in an interactive Terminal window
cd ~/Desktop/News_Spread_Engine

# Activate pyenv
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"

# Run the pipeline
python3 pipeline/10_run_pipeline_tradier.py
