import argparse
import json
from .store import RecordStore
from .capture import capture
from .run import run


def main(argv=None):
    parser = argparse.ArgumentParser(description='跨境履约轨迹乱序归并')
    parser.add_argument("--database", default="collector.sqlite3")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--capture", metavar="FIXTURE_ROOT")
    parser.add_argument("--route", default='/carrier')
    parser.add_argument("--observed-at", default="2026-11-11T10:00:00Z")
    parser.add_argument("--run", metavar="SCENARIO")
    parser.add_argument("--output", metavar="OUTPUT")
    args = parser.parse_args(argv)
    if args.capture:
        print(json.dumps(capture(args.capture, args.route, args.database, args.observed_at), ensure_ascii=False))
        return 0
    if args.run:
        if not args.output:
            parser.error("--run 需要 --output 指定结果文件")
        result = run(args.run, args.database, args.output)
        print(json.dumps({
            "run_id": result["run_id"],
            "parcels": result["parcels"],
            "notifications": result["notifications"],
            "raw_count": result["raw_count"],
        }, ensure_ascii=False))
        return 0
    if args.init:
        store = RecordStore(args.database)
        store.initialize()
        print(json.dumps({"stored": store.count()}))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
