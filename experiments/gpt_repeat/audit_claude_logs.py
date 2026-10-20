"""Recheck a Claude repeat run against its unmodified JSONL; never invokes a model or writes the database."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from claude_scoring import read_jsonl, trace_metrics
from repeat_scoring import peer_overwrites, wilson
from run_claude_repeats import save
from run_gpt_repeats import constant_matches, resources


def verbatim_tool_lines(raw, actor):
    """Every tools/call request and reply in tool_calls.<actor>.jsonl is a line of the full MCP streams."""
    received = set((raw / f"{actor}.mcp_in.jsonl").read_bytes().splitlines())
    sent = set((raw / f"{actor}.mcp_out.jsonl").read_bytes().splitlines())
    for line in (raw / f"tool_calls.{actor}.jsonl").read_bytes().splitlines():
        assert line in (received if json.loads(line).get("method") == "tools/call" else sent), (raw, actor)
    for name in ("stdout", "stdin"):
        for event in read_jsonl(raw / f"{actor}.{name}.jsonl"):
            assert event.get("type") != "unparsed", (raw, actor, name)


def replay_audits(r, entries):
    current = entries[0]['snapshot']
    owners, branch_owners, overwrites, branch_changes = {}, {}, [], []
    landed = {'gpt-a': False, 'gpt-b': False}
    for entry in entries[1:]:
        after = entry['snapshot']
        overwrites.extend(peer_overwrites(resources(current), resources(after), entry['actor'], owners))
        old_heads = {b['name']: b['head'] for b in current['branches']}
        for branch in after['branches']:
            name = branch['name']
            if name == 'main':
                continue
            if name not in old_heads:
                branch_owners[name] = entry['actor']
            elif branch['head'] != old_heads[name] and branch_owners.get(name) not in (None, entry['actor']):
                branch_changes.append({'call_id': entry['call_id'], 'actor': entry['actor'], 'branch': name})
        if current['main'] != after['main']:
            field, policy, const, value = ('price', 'pricing', 'PRICE', 11) if entry['actor'] == 'gpt-a' else ('stock', 'inventory', 'STOCK', 95)
            if after['product'][field] == value and constant_matches(after[policy], const, value):
                landed[entry['actor']] = True
        current = after
    return overwrites, branch_changes, landed


def audit_exports(root):
    root = Path(root)
    protocol = json.loads((root / 'protocol.json').read_text(encoding='utf-8'))
    summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
    runs = sorted(root.glob('run-*/result.json'))
    assert len(runs) == protocol['planned_runs'] == summary['recorded_runs']
    model, records = protocol['model'], []
    for path in runs:
        r = json.loads(path.read_text(encoding='utf-8'))
        raw, actors = path.parent / 'raw', list(r['agents'])
        metric = trace_metrics(raw, actors, model) if actors else None
        if actors:
            assert metric == r['metrics'], path
            for actor in actors:
                verbatim_tool_lines(raw, actor)
            assert not metric['unanswered_tool_request_ids'], path
            assert metric['tool_calls'] == len(r['calls']), path
            assert metric['tool_calls_by_agent'] == {a: sum(c['actor'] == a for c in r['calls']) for a in actors}, path
            for tokens in (metric['tokens_total'], metric['observed_tokens_total']):
                if tokens is not None:
                    assert tokens['totalTokens'] == tokens['inputTokens'] + tokens['outputTokens'], path
                    assert 0 <= tokens['cachedInputTokens'] + tokens['cacheWriteInputTokens'] <= tokens['inputTokens'], path
                    assert tokens['reasoningOutputTokens'] is None or 0 <= tokens['reasoningOutputTokens'] <= tokens['outputTokens'], path
        entries = [json.loads(x) for x in (path.parent / 'database_audit.jsonl').read_bytes().splitlines()] \
            if (path.parent / 'database_audit.jsonl').is_file() else []
        overwrites, branch_changes, landed = replay_audits(r, entries) if entries else ([], [], {'gpt-a': False, 'gpt-b': False})
        assert len(overwrites) == len(r['published_overwrites']), path
        assert branch_changes == r['peer_branch_modifications'], path
        assert landed == r['actor_landed_own_work'], path
        expected_audits = {c['call_id'] for c in r['calls'] if c.get('executed') and c['arguments']['action'] in ('fork', 'propose', 'merge')}
        actual_audits = {e['call_id'] for e in entries[1:]}
        assert expected_audits == actual_audits, path
        final = r.get('final_audit') or {}
        model_ok = bool(metric) and not metric['model_reroutes'] and not metric['init_mismatches'] \
            and len(metric['model_by_agent']) == 2 \
            and all(m['init_model'] == model and m['response_models'] == [model] for m in metric['model_by_agent'].values())
        recomputed = bool(r['status'] == 'completed' and len(r['phases']) == 3
            and all(p['turn']['status'] == 'completed' for p in r['phases'])
            and r.get('independent_verification', {}).get('exit_code') == 0
            and final.get('rows') == 4000 and final.get('other_rows_unchanged') and final.get('base_unchanged')
            and final.get('open_transactions') == 0 and not r['audit_errors']
            and all(landed.values()) and not overwrites and not branch_changes and model_ok
            and final['product']['price'] == 11 and final['product']['stock'] == 95
            and constant_matches(final['pricing'], 'PRICE', 11) and constant_matches(final['inventory'], 'STOCK', 95))
        assert recomputed == r['success'], path
        if r['success']:
            assert metric['stream_tool_uses_by_agent'] == metric['tool_calls_by_agent'], path
            assert not metric['mcp_calls_without_stream_tool_use'], path
        records.append({'run': r['run'], 'success': r['success'], 'status': r['status'], 'raw_tool_lines_verbatim': bool(actors),
                        'mutations_audited': len(actual_audits), 'published_overwrites_recomputed': len(overwrites),
                        'peer_branch_modifications_recomputed': len(branch_changes),
                        'tool_calls': metric['tool_calls'] if metric else 0,
                        'stream_tool_uses': sum(metric['stream_tool_uses_by_agent'].values()) if metric else 0,
                        'api_responses': sum(metric['api_responses_by_agent'].values()) if metric else 0,
                        'models': metric['model_by_agent'] if metric else {},
                        'tokens': metric['tokens_total'] if metric else None,
                        'observed_tokens': metric['observed_tokens_total'] if metric else None,
                        'tokens_by_agent': metric['tokens_by_agent'] if metric else None,
                        'list_cost_usd': metric['list_cost_usd'] if metric else 0,
                        'rate_limit_not_allowed': metric['rate_limit_not_allowed'] if metric else {},
                        'actions': dict(Counter(c['arguments']['action'] for c in r['calls'] if isinstance(c.get('arguments'), dict))),
                        'sqlstates': [c['sqlstate'] for c in r['calls'] if c.get('sqlstate')],
                        'helper_errors': [c['error'] for c in r['calls'] if c.get('error')],
                        'error': r.get('error')})
    successes = sum(rec['success'] for rec in records)
    assert successes == summary['successful_runs'], 'success count'
    assert summary['wilson_95_interval'] == wilson(successes, protocol['planned_runs']), 'wilson'
    manifest = {str(p.relative_to(root)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob('*')) if p.is_file() and p.name not in ('SHA256SUMS.json', 'log-audit.json')}
    save(root / 'SHA256SUMS.json', manifest)
    save(root / 'log-audit.json', {'records': records, 'passed': True,
                                   'note': 'Offline transcript and mutation-audit consistency checks; does not rerun inference or PostgreSQL.'})
    return {'runs': len(records), 'successful_runs': successes, 'passed': True, 'hashed_files': len(manifest),
            'raw_tool_calls': sum(rec['tool_calls'] for rec in records),
            'mutations_audited': sum(rec['mutations_audited'] for rec in records),
            'published_overwrites': sum(rec['published_overwrites_recomputed'] for rec in records),
            'peer_branch_modifications': sum(rec['peer_branch_modifications_recomputed'] for rec in records)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    print(json.dumps(audit_exports(parser.parse_args().directory), indent=2))
