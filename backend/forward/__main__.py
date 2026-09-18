"""Explicit local CLI; no web endpoint, deployment hook, or broker dependency."""
import argparse,json,logging
from pathlib import Path
from .contracts import ProviderSpec
from .runner import ForwardRunner


def main():
    parser=argparse.ArgumentParser(description='Local real-data forward DEMO only')
    parser.add_argument('--database',required=True,help='Dedicated new forward SQLite path')
    parser.add_argument('--config',required=True,help='JSON array of provider specifications; no secret values')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--status',action='store_true')
    args=parser.parse_args()
    logging.basicConfig(level=logging.WARNING,format='%(levelname)s %(name)s %(message)s')
    try:
        specs=[ProviderSpec(**r) for r in json.loads(Path(args.config).read_text(encoding='utf-8'))]
        runner=ForwardRunner(args.database,specs)
        if args.status:print(json.dumps(runner.status(),sort_keys=True))
        elif args.once:
            runner.once();print(json.dumps(runner.status(),sort_keys=True))
        else:runner.run()
    except KeyboardInterrupt:pass
    except Exception:raise SystemExit('FORWARD_START_OR_RUN_FAILED: inspect configuration and readiness; sensitive details suppressed') from None


if __name__=='__main__':main()
