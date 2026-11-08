from aletheia.retrieval import recall
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate


def test_receipt_tier_tracks_only_returned_items(tmp_path):
    sub = Substrate(str(tmp_path / 'db'), ScopeEngine(known_scopes={'a'}))
    ctx = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    sub.put(Record('strong', 'Source', 'a', {'text': 'needle ' * 10}, attribution='user'), ctx)
    sub.put(Record('deep', 'Source', 'a', {'text': 'x' * 450 + ' needle'}, attribution='user'), ctx)
    items, receipt = recall(sub, 'needle', ctx, mode='lexical', limit=1)
    assert [x.record_id for x in items] == ['strong']
    assert items[0].tier == 'L0'
    assert receipt.tier == 'L0'
