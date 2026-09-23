"""Opt-in real-provider benchmark; input JSONL contains title/description/source_lang.

DeepL runs consume quota. Run from the project root with the same configured model
as production. This reports runtime measurements, not semantic quality scores.
"""
from __future__ import annotations

import argparse
import json
import math
import platform
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from yt2bili.config import load_settings
from yt2bili.translation.service import translate
from yt2bili.translation.runtime import manifest
from yt2bili.exceptions import Yt2BiliError


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('samples',type=Path)
    parser.add_argument('--provider',choices=('local_llm','deepl'),default='local_llm')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    settings=replace(load_settings(),translation_primary=args.provider,translation_fallback_enabled=False)
    samples=[json.loads(line) for line in args.samples.read_text(encoding='utf-8').splitlines() if line.strip()]
    if not samples:parser.error('样本不能为空')
    records=[]
    for index,sample in enumerate(samples):
        started=time.monotonic()
        try:
            result=translate(settings,sample['title'],sample.get('description',''),sample.get('source_lang'),80,1800)
            row={'index':index,'ok':True,'result':asdict(result)}
        except Yt2BiliError as exc:
            row={'index':index,'ok':False,'error':str(exc)}
        row['wall_ms']=round((time.monotonic()-started)*1000)
        records.append(row)
        print(f"{index+1}/{len(samples)} {'OK' if row['ok'] else 'FAIL'} {row['wall_ms']} ms",flush=True)
    durations=sorted(row['wall_ms'] for row in records)
    report={'platform':platform.platform(),'cpu':platform.processor(),'provider':args.provider,
            'manifest':manifest(),'count':len(records),'failed':sum(not row['ok'] for row in records),
            'p50_all_ms':durations[max(0,math.ceil(len(durations)*.5)-1)],
            'p95_all_ms':durations[max(0,math.ceil(len(durations)*.95)-1)],
            'quality_review_required':True,'below_prd_sample_count':len(records)<100,'records':records}
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 1 if report['failed'] else 0


if __name__=='__main__':raise SystemExit(main())
