"""Reproducible, offline performance measurements for #42.

The command deliberately reports observations instead of pass/fail thresholds.  It
is usable from the baseline checkout and from later revisions with the same data.
"""
from __future__ import annotations
import argparse, json, os, platform, statistics, sys, tempfile, time
from pathlib import Path

DATASET_VERSION = 1

class CountingEmbedding:
    def __init__(self, dimensions): self.dimensions, self.calls = dimensions, 0
    def embed(self, text, model=None):
        self.calls += 1
        return [1.0] + [0.0] * (self.dimensions - 1)

def clocked(fn):
    start = time.perf_counter(); value = fn(); return value, (time.perf_counter()-start)*1000

def memory_rows(target: Path, scale: int, repeats: int):
    sys.path.insert(0, str(target))
    from memory_service import MemoryService
    rows = []
    for scenario, client in [('no-vector', None), ('rebuilding', CountingEmbedding(3)), ('ready', CountingEmbedding(3))]:
        with tempfile.TemporaryDirectory(prefix='jarvis-baseline-memory-') as directory:
            memory = MemoryService(Path(directory), embedding_client=client, embedding_model='synthetic', embedding_dimensions=3)
            for i in range(scale):
                memory.remember({'subject':'USER','predicate':'likes','object':f'topic{i:06d}',
                    'text':f'Synthetic preference topic{i:06d}', 'category':'work_preferences'}, source={
                    'quote':f'I prefer topic{i:06d}', 'recorded_at':'2026-01-01T00:00:00Z',
                    'source_task_id':f'synthetic-task-{i}', 'source_event_id':f'synthetic-event-{i}',
                    'trajectory_path':'synthetic.jsonl'})
            if scenario == 'ready' and client is not None:
                sync = getattr(memory, '_sync_vectors', None)
                if sync is None:
                    sync = memory._retrieval._sync_vectors
                sync()
            for _ in range(repeats):
                before = client.calls if client else 0
                result, elapsed = clocked(lambda: memory.search('topic000000'))
                rows.append({'operation':'memory_search','scenario':scenario,'scale':scale,
                    'elapsed_ms':elapsed,'observed':{'objects':[x['object'] for x in result['facts']],
                    'source_event':result['facts'][0]['sources'][0]['source_event_id'], 'scan_operations':scale,
                    'embedding_calls':(client.calls-before) if client else 0,
                    'vector_available':result['vector_available']}})
            memory.close()
    return rows

def history_rows(target: Path, scale: int, repeats: int):
    sys.path.insert(0, str(target)); from task_history import TaskHistory
    rows=[]
    for _ in range(repeats):
        with tempfile.TemporaryDirectory(prefix='jarvis-baseline-history-') as directory:
            history=TaskHistory(Path(directory), 'synthetic-session', 1)
            for i in range(scale):
                history.record({'type':'task','goal':f'probe-{i}'})
                history.record({'type':'message','message':{'role':'assistant','content':f'answer-{i}'}})
                history.record({'type':'task_end','status':'completed'})
            value, elapsed=clocked(lambda: history.messages())
            text=''.join(p.read_text(encoding='utf-8') for p in (Path(directory)/'history').glob('*.md'))
            rows.append({'operation':'history_task','scenario':'projection','scale':scale,'elapsed_ms':elapsed,
                'observed':{'history_has_probe':'probe-0' in text,'history_has_answer':'answer-0' in text,
                'recent_has_answer':f'answer-{scale-1}' in json.dumps(value), 'scan_operations':scale * 3}})
    return rows

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--target',type=Path,required=True); parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--scales',type=int,default=32); parser.add_argument('--repeats',type=int,default=5); parser.add_argument('--sections',default='memory,history,shell')
    args=parser.parse_args(); sections=set(args.sections.split(',')); rows=[]
    if 'memory' in sections: rows += memory_rows(args.target,args.scales,args.repeats)
    if 'history' in sections: rows += history_rows(args.target,args.scales,args.repeats)
    shell={'supported':os.name=='nt','note':'Windows AppContainer preparation is measured only on Windows; no unsandboxed substitute is used.'}
    if 'shell' in sections and os.name == 'nt':
        from shell_sandbox import start_shell
        with tempfile.TemporaryDirectory(prefix='jarvis-baseline-shell-') as workspace, tempfile.TemporaryDirectory(prefix='jarvis-baseline-state-') as protected:
            output_path=Path(workspace)/'shell.out'
            with output_path.open('wb') as output:
                started=time.perf_counter(); process=start_shell('echo baseline', workspace, protected, output); preparation=(time.perf_counter()-started)*1000
            process.start(); executed=time.perf_counter()
            while process.poll() is None: time.sleep(0.001)
            execution=(time.perf_counter()-executed)*1000
            process.close()
            shell.update({'preparation_ms':preparation,'execution_ms':execution,'command':'echo baseline'})
    report={'dataset':{'version':DATASET_VERSION,'scale':args.scales,'repeats':args.repeats,'seed':'fixed synthetic literals'},
      'environment':{'python':sys.version,'platform':platform.platform(),'commit':os.environ.get('BASELINE_COMMIT','unknown')},
      'methodology':{'warmup':'none; each temporary dataset is cold, repeated operations are warm within a run','timing':'perf_counter wall time; filesystem and sqlite included','limits':'single process, local temporary storage, synthetic client; no arbitrary threshold'},
      'measurements':rows,'shell_preparation':shell}
    args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(report,indent=2),encoding='utf-8'); print(json.dumps(report,indent=2))
if __name__=='__main__': main()
