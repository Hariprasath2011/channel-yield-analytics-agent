"""Smoke tests for the read-only HTTP API; uses only synthetic fixture data."""
import json, os, tempfile, threading, urllib.request, urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
from analytics_api import Handler
from csv_ingestion_pipeline import run_pipeline

ROOT=Path(__file__).parent

def request(url, token=None, method="GET"):
    headers={}
    if token: headers["Authorization"]="Bearer "+token
    req=urllib.request.Request(url,headers=headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=4) as r: return r.status,json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code,json.loads(e.read())

def main():
    tests=[]
    with tempfile.TemporaryDirectory() as td:
        db=Path(td)/"api.sqlite"
        run_pipeline(ROOT/"ingestion_config_demo.json",db,ROOT)
        os.environ["CY_API_TOKEN"]="test-secret-strong-enough"
        os.environ["CY_CLIENT_ID"]="demo_client"
        os.environ["CY_DB_PATH"]=str(db)
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        base=f"http://127.0.0.1:{server.server_port}"
        try:
            s,b=request(base+"/health"); tests.append(("health endpoint",s==200 and b.get("status")=="ok",s))
            s,b=request(base+"/v1/analytics"); tests.append(("missing token rejected",s==401 and b.get("error")=="unauthorized",s))
            s,b=request(base+"/v1/analytics","wrong-token"); tests.append(("invalid token rejected",s==401,s))
            s,b=request(base+"/v1/analytics?cohort_start=2026-01-01&cohort_end=2026-06-30&as_of_date=2026-10-11&observation_days=90","test-secret-strong-enough")
            tests.append(("authorized analytics returns aggregate JSON",s==200 and b.get("contract_version")=="analytics_response_v1" and "channel_metrics" in b,s))
            s,b=request(base+"/v1/analytics?observation_days=-2","test-secret-strong-enough"); tests.append(("invalid policy rejected",s==400 and b.get("error")=="invalid_request",s))
            s,b=request(base+"/v1/analytics?badparam=x","test-secret-strong-enough"); tests.append(("unknown query parameter rejected",s==400 and b.get("error")=="unknown_parameters",s))
            s,b=request(base+"/v1/analytics?requisition_id=DOES-NOT-EXIST","test-secret-strong-enough"); tests.append(("unknown requisition gives 404",s==404 and b.get("status")=="not_found",s))
            s,b=request(base+"/v1/analytics","test-secret-strong-enough",method="POST"); tests.append(("write method rejected",s==405,s))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
    result={"passed":sum(1 for _,ok,_ in tests if ok),"total":len(tests),"tests":[{"name":n,"passed":ok,"detail":d} for n,ok,d in tests]}
    (ROOT/"analytics_api_test_output.json").write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
    if result["passed"]!=result["total"]: raise SystemExit(1)
if __name__=="__main__": main()
