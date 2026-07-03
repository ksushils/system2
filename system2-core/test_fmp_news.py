import json, sys, os
os.environ['PYTHONIOENCODING'] = 'utf-8'

# Monkey-patch the input path
import fmp_news_analyst_signals as mod
mod.INPUT_PATH = mod.ROOT / 'test_stage2_3tickers.json'
mod.OUTPUT_PATH = mod.ROOT / 'test_fmp_news_output.json'
mod.METADATA_PATH = mod.ROOT / 'test_fmp_news_metadata.json'

mod.main()
