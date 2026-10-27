import argparse
import json
from pathlib import Path
from . import pipeline, server

def main():
    parser=argparse.ArgumentParser(description="Local hot-list workbench (Python 3.9+, standard library)")
    sub=parser.add_subparsers(dest="command",required=True)
    for name in ("collect","serve"):
        p=sub.add_parser(name);p.add_argument("--db",default="data/workbench.sqlite3")
        if name=="serve": p.add_argument("--host",default="127.0.0.1");p.add_argument("--port",type=int,default=8765)
    args=parser.parse_args(); db=str(Path(args.db).resolve())
    if args.command=="collect": print(json.dumps(pipeline.run(db),ensure_ascii=False,indent=2))
    else: server.serve(args.host,args.port,db)

if __name__ == "__main__": main()
