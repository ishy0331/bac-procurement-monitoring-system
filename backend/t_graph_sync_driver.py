"""Real driver (6.4.1 source) <-> Neo4j's scripted Bolt test server (testkit boltstub).
Checks graph_sync's driver calls (_run in managed read/write transactions with unit_of_work,
_run_auto for schema commands): exact Cypher text and parameters on the wire, record data,
summary counters, Date/float/int parameter types. SIMULATED server (no real Neo4j)."""
import json, os, socket, subprocess, sys, time
from datetime import date
S = os.environ['SCRATCH']; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backend'))
import neo4j, graph_sync as GS
fn = GS._foreign_nodes('Procurement'); mn = GS._merge_nodes('Procurement')
rows = [{"key": 31, "props": {"title": "Office supplies", "abc": 12345.67, "fiscalYear": 2026, "controlNo": None}}]
bd = GS._merge_nodes('BidNotice'); brow = [{"key": 5, "props": {"submissionDeadline": date(2026, 11, 30)}}]
q = lambda s: json.dumps(s)
script = f'''!: BOLT 6.0

C: HELLO {{"{{}}": "*"}}
S: SUCCESS {{"server": "Neo4j/2026.10.1", "connection_id": "bolt-1"}}
C: LOGON {{"scheme": "basic", "principal": "u", "credentials": "p"}}
S: SUCCESS {{}}
*: RESET
C: BEGIN {{"{{}}": "*"}}
S: SUCCESS {{}}
C: RUN {q(fn.text)} {{"owner": "santa-rosa-bac-pms"}} {{"{{}}": "*"}}
S: SUCCESS {{"fields": ["c"]}}
C: PULL {{"n": {{"Z": "*"}}, "[qid]": {{"Z": "*"}}}}
S: RECORD [0]
   SUCCESS {{"type": "r"}}
C: COMMIT
S: SUCCESS {{}}
*: RESET
C: BEGIN {{"{{}}": "*"}}
S: SUCCESS {{}}
C: RUN {q(mn.text)} {{"owner": "santa-rosa-bac-pms", "runId": "run-1", "rows": [{{"key": 31, "props": {{"title": "Office supplies", "abc": {{"R": "12345.67"}}, "fiscalYear": 2026, "controlNo": null}}}}]}} {{"{{}}": "*"}}
S: SUCCESS {{"fields": ["n"]}}
C: PULL {{"n": {{"Z": "*"}}, "[qid]": {{"Z": "*"}}}}
S: RECORD [1]
   SUCCESS {{"type": "w", "stats": {{"nodes-created": 1, "properties-set": 6, "labels-added": 2}}}}
C: RUN {q(bd.text)} {{"owner": "santa-rosa-bac-pms", "runId": "run-1", "rows": [{{"key": 5, "props": {{"submissionDeadline": {{"T": "2026-11-30"}}}}}}]}} {{"{{}}": "*"}}
S: SUCCESS {{"fields": ["n"]}}
C: PULL {{"n": {{"Z": "*"}}, "[qid]": {{"Z": "*"}}}}
S: RECORD [1]
   SUCCESS {{"type": "w", "stats": {{"nodes-created": 0, "properties-set": 3}}}}
C: COMMIT
S: SUCCESS {{}}
*: RESET
C: RUN {q(GS.SHOW_CONSTRAINTS.text)} {{}} {{"{{}}": "*"}}
S: SUCCESS {{"fields": ["name", "type", "entityType", "labelsOrTypes", "properties"]}}
C: PULL {{"n": {{"Z": "*"}}, "[qid]": {{"Z": "*"}}}}
S: RECORD ["bac_pms_procurement_procurement_id_unique", "NODE_PROPERTY_UNIQUENESS", "NODE", ["Procurement"], ["procurementId"]]
   SUCCESS {{"type": "r"}}
*: RESET
?: GOODBYE
'''
s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
path = os.path.join(S, 'sync_driver.script'); open(path, 'w').write(script)
log = open(os.path.join(S, 'sync_driver_stub.log'), 'w')
env = dict(os.environ, PYTHONPATH=os.path.join(S, 'testkit') + ':' + os.path.join(S, 'libstub'))
p = subprocess.Popen(['python3', '-m', 'boltstub', '-l', f'127.0.0.1:{port}', path], cwd=os.path.join(S, 'testkit'), env=env, stdout=log, stderr=subprocess.STDOUT)
time.sleep(1.5)
RES = []
def check(n, c, e=''): RES.append(c); print(f"  [{'PASS' if c else 'FAIL'}] {n}" + (f'  ({e})' if e != '' else ''))
drv = neo4j.GraphDatabase.driver(f'bolt://127.0.0.1:{port}', auth=('u', 'p'))
try:
    with drv.session(database='neo4j') as sess:
        recs, cnt = sess.execute_read(lambda tx: GS._run(tx, fn, {"owner": GS.OWNER}))
        check('read transaction: exact Cypher + parameters accepted, record data returned', recs == [{"c": 0}], recs)
        out = sess.execute_write(GS._with_timeout(lambda tx: (GS._run(tx, mn, {"owner": GS.OWNER, "runId": "run-1", "rows": rows}),
                                                              GS._run(tx, bd, {"owner": GS.OWNER, "runId": "run-1", "rows": brow}))))
        (r1, c1), (r2, c2) = out
        check('write transaction (unit_of_work timeout): float, int, null and Date parameters sent as Bolt types', r1 == [{"n": 1}] and r2 == [{"n": 1}])
        check('summary counters read correctly (nodes_created 1, then 0)', c1.nodes_created == 1 and c2.nodes_created == 0, (c1.nodes_created, c2.nodes_created))
        recs, _ = GS._run_auto(sess, GS.SHOW_CONSTRAINTS, {})
        check('auto-commit schema command: SHOW CONSTRAINTS rows parsed', recs and recs[0]['labelsOrTypes'] == ['Procurement'] and GS._is_unique(recs[0]) and GS._constraint_matches(recs[0], 'Procurement', 'procurementId'), recs)
finally:
    drv.close(); time.sleep(0.5); p.terminate(); p.wait(5)
txt = open(log.name).read()
check('scripted server saw the whole conversation with no mismatch', 'Script mismatch' not in txt and 'Traceback' not in txt.split('Listening', 1)[-1], txt[-600:] if 'mismatch' in txt.lower() else '')
print(f'\nTOTAL: {sum(RES)}/{len(RES)} passed  (SIMULATED server, real driver)')
