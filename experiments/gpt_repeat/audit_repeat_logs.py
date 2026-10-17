"""Recheck results against unmodified JSONL; never invokes a model or writes the database."""
import argparse
import hashlib
import json
from pathlib import Path

from repeat_scoring import peer_overwrites, trace_metrics
from run_gpt_repeats import constant_matches, resources, save


def audit_exports(root):
    root = Path(root)
    protocol = json.loads((root / 'protocol.json').read_text(encoding='utf-8'))
    summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
    runs = sorted(root.glob('run-*/result.json'))
    assert len(runs) == protocol['planned_runs'] == summary['recorded_runs']
    records = []
    for path in runs:
        r = json.loads(path.read_text(encoding='utf-8'))
        raw = path.parent / 'raw'
        metric = trace_metrics(raw, list(r['threads'].values()))
        assert metric == r['metrics'], path
        received = (raw/'server.jsonl').read_bytes().splitlines()
        sent = (raw/'client.jsonl').read_bytes().splitlines()
        raw_tools = (raw/'tool_calls.jsonl').read_bytes().splitlines()
        for line in raw_tools:
            event = json.loads(line)
            assert line in (received if event.get('method') == 'item/tool/call' else sent), path
        entries = [json.loads(x) for x in (path.parent/'database_audit.jsonl').read_bytes().splitlines()]
        current = entries[0]['snapshot']
        owners, branch_owners, recomputed, branch_changes = {}, {}, [], []
        actor_landed = {'gpt-a': False, 'gpt-b': False}
        for entry in entries[1:]:
            after = entry['snapshot']
            recomputed.extend(peer_overwrites(resources(current),resources(after),entry['actor'],owners))
            old_heads = {b['name']: b['head'] for b in current['branches']}
            for branch in after['branches']:
                name = branch['name']
                if name == 'main':
                    continue
                if name not in old_heads:
                    branch_owners[name] = entry['actor']
                elif branch['head'] != old_heads[name] and branch_owners.get(name) not in (None, entry['actor']):
                    branch_changes.append({'call_id':entry['call_id'],'actor':entry['actor'],'branch':name})
            if current['main'] != after['main']:
                field,policy,const,value = ('price','pricing','PRICE',11) if entry['actor']=='gpt-a' else ('stock','inventory','STOCK',95)
                if after['product'][field] == value and constant_matches(after[policy],const,value):
                    actor_landed[entry['actor']] = True
            current = after
        assert len(recomputed) == len(r['published_overwrites']), path
        assert branch_changes == r['peer_branch_modifications'], path
        assert actor_landed == r['actor_landed_own_work'], path
        expected_audits = {c['call_id'] for c in r['calls'] if c.get('executed') and c['arguments']['action'] in ('fork','propose','merge')}
        actual_audits = {e['call_id'] for e in entries[1:]}
        assert expected_audits == actual_audits, path
        assert not metric['unanswered_tool_request_ids'], path
        assert metric['tool_calls'] == len(r['calls']), path
        assert all(m['model']==protocol['model'] and m['reasoningEffort']==protocol['reasoning_effort']
                   for m in metric['model_by_thread'].values()), path
        tokens = metric['tokens_total']
        if tokens is not None:
            assert tokens['totalTokens'] == tokens['inputTokens'] + tokens['outputTokens'], path
            assert 0 <= tokens['cachedInputTokens'] <= tokens['inputTokens'], path
            assert 0 <= tokens['reasoningOutputTokens'] <= tokens['outputTokens'], path
        if r['success']:
            final = r['final_audit']
            assert r['status']=='completed' and len(r['phases'])==3, path
            assert all(p['turn']['status']=='completed' for p in r['phases']), path
            assert all(actor_landed.values()) and not recomputed and not branch_changes, path
            assert final['product']['price']==11 and final['product']['stock']==95, path
            assert constant_matches(final['pricing'],'PRICE',11) and constant_matches(final['inventory'],'STOCK',95), path
            assert final['rows']==4000 and final['other_rows_unchanged'] and final['base_unchanged'], path
            assert final['open_transactions']==0 and not r['audit_errors'], path
            assert not metric['model_reroutes'] and len(metric['model_by_thread'])==2, path
            assert r['independent_verification']['exit_code']==0, path
        records.append({'run':r['run'],'raw_tool_lines_verbatim':True,
                        'mutations_audited':len(actual_audits),'published_overwrites_recomputed':len(recomputed),
                        'tool_calls':metric['tool_calls'],'model':list(metric['model_by_thread'].values()),
                        'token_metrics_available':tokens is not None})
    assert sum(json.loads(p.read_text(encoding='utf-8'))['success'] for p in runs) == summary['successful_runs']
    manifest = {str(p.relative_to(root)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob('*')) if p.is_file() and p.name not in ('SHA256SUMS.json','log-audit.json')}
    save(root/'SHA256SUMS.json',manifest)
    save(root/'log-audit.json',{'records':records,'passed':True,'note':'Offline transcript and mutation-audit consistency checks; does not rerun inference or PostgreSQL.'})
    return {'runs':len(records),'passed':True,'hashed_files':len(manifest),
            'raw_tool_calls':sum(r['tool_calls'] for r in records),'mutations_audited':sum(r['mutations_audited'] for r in records)}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory',type=Path)
    print(json.dumps(audit_exports(p.parse_args().directory),indent=2))
