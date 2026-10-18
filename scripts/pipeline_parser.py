"""Весь пайплайн (ядро + OSM) в виде «парсера» для core/evaluate.py:

    cd core && python evaluate.py --parser ../scripts/pipeline_parser.py \
        --input ../data/synthetic/synth_input.json --gold ../data/synthetic/synth_gold.json
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import process_batch  # noqa: E402


def parse_batch(records):
    return process_batch(records)
