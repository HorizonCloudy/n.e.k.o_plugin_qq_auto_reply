from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.qq_auto_reply.pipeline_models import QQDeliveryPlan, QQMessageBlock
from plugin.plugins.qq_auto_reply.reply_delivery_node import QQReplyDeliveryNode
from plugin.plugins.qq_auto_reply.reply_sentence_split import split_sentences, split_text_blocks


def test_each_sentence_is_split_regardless_of_length():
    text = '这是第一句' * 12 + '。' + '这是第二句' * 12 + '！'
    parts = split_sentences(text)
    assert len(parts) == 2
    assert ''.join(parts) == text
    assert split_sentences('你好。今天开心吗？') == ['你好。', '今天开心吗？']
    assert split_sentences('长' * 150) == ['长' * 150]


@pytest.mark.parametrize('protected', [
    'https://example.com/watch?a=1.2&b=yes!ok',
    '[链接](https://example.com/a?b=1)',
    '```python\nprint("你好！")\n```',
    '`a!b?c`',
])
def test_protected_content_is_not_cut(protected):
    text = '开头' * 55 + '。' + protected + '\n结尾。'
    parts = split_sentences(text)
    assert any(protected in part for part in parts)


def test_metadata_not_duplicated_and_media_unchanged():
    original = QQMessageBlock(text='甲' * 55 + '。' + '乙' * 55 + '。', at_user='1', emoji='3')
    parts = split_text_blocks([original])
    assert [p.at_user for p in parts] == ['1', '']
    assert [p.reply_to for p in parts] == ['', '']
    assert [p.emoji for p in parts] == ['', '3']
    assert [p.text for p in parts] == ['甲' * 55, '乙' * 55]
    voice = QQMessageBlock(record=original.text)
    assert split_text_blocks([voice]) == [voice]


def test_quoted_reply_is_not_split_or_rewritten():
    quoted = QQMessageBlock(text='知道啦。等我一下！', reply_to='42', at_user='1', emoji='3')
    assert split_text_blocks([quoted]) == [quoted]


@pytest.mark.parametrize('text,expected', [
    ('你好。今天开心吗？太好了！', ['你好', '今天开心吗？', '太好了！']),
    ('Hello. Bye!', ['Hello', 'Bye!']),
    ('她说：“好。”\n等一下……', ['她说：“好”', '等一下……']),
    ('开头。https://example.com/a.\n`code.`\n3.14\n等等...',
     ['开头', 'https://example.com/a.', '`code.`', '3.14', '等等...']),
    ('只有一句。', ['只有一句。']),
])
def test_split_bubbles_remove_only_terminal_full_stops(text, expected):
    assert [b.text for b in split_text_blocks([QQMessageBlock(text=text)])] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled,failed', [(False,False), (True,False), (True,True)])
async def test_delivery_switch_and_partial_failure(monkeypatch, enabled, failed):
    monkeypatch.setattr('plugin.plugins.qq_auto_reply.reply_delivery_node.asyncio.sleep', AsyncMock())
    plugin = SimpleNamespace(_qq_settings={'force_sentence_split': enabled},
                             _get_reply_mode=lambda: 'text', logger=Mock())
    node = QQReplyDeliveryNode(plugin)
    node._send_text = AsyncMock(side_effect=[True, False] if failed else None, return_value=True)
    text = '你好。今天开心吗？'
    plan = QQDeliveryPlan('private', '123', blocks=[QQMessageBlock(text=text)])
    result = await node.deliver(plan)
    assert result.delivered is not failed
    assert node._send_text.await_count == (2 if enabled else 1)
    assert [call.args[2] for call in node._send_text.call_args_list] == (['你好', '今天开心吗？'] if enabled else [text])
    assert plan.blocks[0].text == text
