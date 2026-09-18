"""Local reports/runner. The shipped configuration disables acquisition."""
import argparse,json,logging
from pathlib import Path
from .config import NativeSpec
from .runner import RealRunner
from .reports import report,capacity
from .forexfactory import ForexFactoryAdapter,ForexFactoryConfig


def main():
    parser=argparse.ArgumentParser(description='Phase 3F local DEMO only; no broker integration')
    parser.add_argument('--config',required=True);parser.add_argument('--database',required=True)
    parser.add_argument('--action',choices=('status','once','run','report','capacity'),default='status')
    args=parser.parse_args();logging.basicConfig(level=logging.WARNING,format='%(levelname)s %(name)s %(message)s')
    try:
        config=json.loads(Path(args.config).read_text(encoding='utf-8'))
        if set(config)-{'collection_enabled','providers','forex_factory'}:raise ValueError('UNKNOWN_CONFIGURATION_FIELDS')
        runner=RealRunner(args.database,[NativeSpec(**s) for s in config.get('providers',[])],collection_enabled=config.get('collection_enabled',False),secondary=ForexFactoryAdapter(ForexFactoryConfig(**config.get('forex_factory',{}))))
        if args.action=='run':
            if not runner.collection_enabled:raise ValueError('COLLECTION_DISABLED')
            runner.run();return
        if args.action=='once':runner.once()
        result=report(runner.store,runner.clock()) if args.action=='report' else capacity(runner.store) if args.action=='capacity' else runner.status()
        print(json.dumps(result,sort_keys=True,allow_nan=False))
    except KeyboardInterrupt:pass
    except Exception:raise SystemExit('PHASE3F_OPERATION_FAILED: credentials and provider details suppressed') from None


if __name__=='__main__':main()
