"""Isolated editorial revision; never touches the publication queue."""
import argparse
import shutil
import logging
import story_batch_two as stories

batch = stories.batch
engine = stories.engine
KEY = 'choose_fear_recut'
OUT = batch.ROOT / 'review/choose_fear_rework_2026-11-04'
PLAN = dict(episode=3, title='Он боялся назвать имя. Томми дал минуту на выбор',
    segments=[(2779.72,2782.12),(2791.96,2796.16),(2799.60,2803.96),
              (2804.68,2815.76),(2817.88,2822.60)],
    gamma=1.18, music='jamendo_b6bccae4', bed=-24)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['prepare','render','check'])
    args=parser.parse_args()
    stories.OUT=OUT
    stories.PLANS={KEY:PLAN}
    stories.select(KEY)
    folder=OUT/KEY
    folder.mkdir(parents=True,exist_ok=True)
    if args.action=='prepare':
        shutil.copy2(batch.ROOT/'review/s03_remaining/e03_choose_fear/context_transcript.json',
                     folder/'context_transcript.json')
        batch.prepare(KEY)
        stories.captions(KEY,batch.whisper())
    elif args.action=='render':
        batch.render(KEY)
        engine.finalize_music(KEY)
    else:
        engine.check_final(KEY,batch.whisper())


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    main()
