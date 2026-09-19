"""Offline reproducible baseline measurements for #42."""
from __future__ import annotations
import argparse, importlib, json, os, platform, subprocess, sys, tempfile, time
from pathlib import Path
DATASET_VERSION=2
class CountingEmbedding:
 def __init__(self,d): self.dimensions,self.calls=d,0
 def embed(self,text,model=None): self.calls+=1; return [1.0]+[0.0]*(self.dimensions-1)
def timed(fn):
 s=time.perf_counter(); v=fn(); return v,(time.perf_counter()-s)*1000
def revision(t):
 try:return subprocess.check_output(['git','-C',str(t),'rev-parse','HEAD'],text=True).strip()
 except Exception:return 'unavailable'
def memory(target,scale,repeats):
 sys.path.insert(0,str(target)); M=importlib.import_module('memory.memory_service' if (target / 'memory').is_dir() else 'memory_service').MemoryService; out=[]
 for scenario,client in [('no-vector',None),('rebuilding',CountingEmbedding(3)),('ready',CountingEmbedding(3))]:
  with tempfile.TemporaryDirectory() as d:
   m=M(Path(d),embedding_client=client,embedding_model='synthetic',embedding_dimensions=3)
   for i in range(scale): m.remember({'subject':'USER','predicate':'likes','object':f'topic{i:06d}','text':f'Synthetic preference topic{i:06d}','category':'work_preferences'},source={'quote':f'I prefer topic{i:06d}','recorded_at':'2026-01-01T00:00:00Z','source_task_id':f'synthetic-task-{i}','source_event_id':f'synthetic-event-{i}','trajectory_path':'synthetic.jsonl'})
   if scenario=='ready':
    retrieval=getattr(m,'_retrieval',None)
    if retrieval is not None and hasattr(retrieval,'_sync_vectors'): retrieval._sync_vectors()
   for _ in range(repeats):
    b=client.calls if client else 0; r,e=timed(lambda:m.search('topic000000')); out.append({'operation':'memory_search','scenario':scenario,'scale':scale,'elapsed_ms':e,'observed':{'objects':[x['object'] for x in r['facts']],'embedding_calls':client.calls-b if client else 0,'facts_returned':len(r['facts']),'source_event':r['facts'][0]['sources'][0]['source_event_id'],'vector_available':r['vector_available']}})
   m.close()
 return out
def projections(target,scale,repeats):
 sys.path.insert(0,str(target)); mod=importlib.import_module('session.task_history' if (target / 'session').is_dir() else 'task_history'); T=mod.TaskHistory; out=[]
 for _ in range(repeats):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); h=T(root,'synthetic-session',scale)
   for i in range(scale): h.record({'type':'task','goal':f'probe-{i}'}); h.record({'type':'message','message':{'role':'assistant','content':f'answer-{i}'}}); h.record({'type':'task_end','status':'completed'})
   rebuild=getattr(mod,'rebuild_projections',getattr(mod,'project_history',lambda directory: None))
   v,e=timed(lambda:rebuild(root,None)); text=''.join(p.read_text() for p in (root/'history').glob('*.md')); out.append({'operation':'history_task','scenario':'projection','scale':scale,'elapsed_ms':e,'observed':{'history_tasks':text.count('task='),'history_has_probe':'probe-0' in text,'history_has_answer':'answer-0' in text,'recent_has_answer':f'answer-{scale-1}' in (root/'recent'/'synthetic-session'/'recent.md').read_text()}})
 return out
def main():
 p=argparse.ArgumentParser(); p.add_argument('--target',type=Path,required=True); p.add_argument('--output',type=Path,required=True); p.add_argument('--scales',default='32'); p.add_argument('--repeats',type=int,default=5); p.add_argument('--sections',default='memory,history,shell'); a=p.parse_args(); t=a.target.resolve(); rows=[]
 for s in map(int,a.scales.split(',')):
  if 'memory' in a.sections: rows+=memory(t,s,a.repeats)
  if 'history' in a.sections: rows+=projections(t,s,a.repeats)
 report={'dataset':{'version':DATASET_VERSION,'scales':list(map(int,a.scales.split(','))),'repeats':a.repeats,'seed':'fixed synthetic literals'},'environment':{'python':sys.version,'platform':platform.platform(),'target':str(t),'commit':revision(t)},'methodology':{'timing':'perf_counter wall time; SQLite/filesystem included','counters':'observed embedding calls and projection outputs; no inferred scan counts'},'measurements':rows,'shell_preparation':{'supported':False,'reason':'shell measurement requires Windows target imports'} if 'shell' in a.sections and os.name!='nt' else None}; a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(report,indent=2)); print(json.dumps(report,indent=2))
if __name__=='__main__': main()
