"""Explicit local CLI; imports never start a service or access a provider."""
import argparse,json
from pathlib import Path
from .data import Split
from .lab import run,sensitivity,reproduce
from .archive import Archive,storage_profile
from .models import MODELS,ABLATIONS
from .execution import STRESSES
from ..domain import canonical


def main():
    parser=argparse.ArgumentParser(description='Offline Phase 3D validation lab; never promotes or deploys')
    parser.add_argument('command',choices=('run','sensitivity','profile','replay'))
    parser.add_argument('--data')
    parser.add_argument('--attempt',type=int)
    parser.add_argument('--lab',required=True)
    parser.add_argument('--split',help='JSON file containing Split fields')
    parser.add_argument('--stage',choices=('validation','holdout'),default='validation')
    parser.add_argument('--allow-test-data',action='store_true')
    parser.add_argument('--all-ablations',action='store_true')
    parser.add_argument('--all-stresses',action='store_true')
    args=parser.parse_args()
    if args.command=='replay':
        if args.attempt is None:parser.error('--attempt required')
        print(canonical(reproduce(args.lab,args.attempt)))
        return
    if not args.data:parser.error('--data required')
    tape=json.loads(Path(args.data).read_text(encoding='utf-8'))
    if args.command=='profile':
        from .data import validate_tape
        normalized,_=validate_tape(tape,args.allow_test_data)
        report=storage_profile(normalized['ticks'],Archive(Path(args.lab)/'profile-objects'))
    else:
        if not args.split:parser.error('--split required')
        split=Split(**json.loads(Path(args.split).read_text(encoding='utf-8')))
        if args.command=='sensitivity':
            if args.stage=='holdout':parser.error('Sensitivity cannot access holdout')
            report=sensitivity(tape,split,args.lab,allow_test=args.allow_test_data)
        else:
            models=(*MODELS,*(('WITHOUT_'+a for a in ABLATIONS) if args.all_ablations else ()))
            report=run(tape,split,args.lab,stage=args.stage,models=models,
                       scenarios=STRESSES if args.all_stresses else ('BASE',),allow_test=args.allow_test_data)
    # Output contains metrics/provenance, never provider credentials.
    print(canonical(report))


if __name__=='__main__':main()
