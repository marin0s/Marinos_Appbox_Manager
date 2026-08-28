from html.parser import HTMLParser

import pytest

from app import main


class Layout(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.rows = []
        self.wrappers = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag != 'div':
            return
        if 'data-node-dashboard' in attrs:
            self.wrappers += 1
            assert attrs['class'] == 'grid'
        if self.stack and 'data-node-dashboard' in self.stack[-1]:
            self.rows.append(attrs.get('class'))
        self.stack.append(attrs)

    def handle_endtag(self, tag):
        if tag == 'div':
            self.stack.pop()


@pytest.mark.parametrize('local', [False, True])
def test_node_rows_share_existing_grid_spacing_without_changing_local_layout(local):
    html = main.templates.get_template('node.html').render(
        mode='mock', hostname='cronos', node={
            'is_local': local, 'name': 'Node test', 'node_id': 'testnode',
            'status': 'online', 'metrics': {}, 'metrics_stale': False,
        })
    layout = Layout()
    layout.feed(html)
    assert layout.wrappers == (0 if local else 1)
    assert layout.rows == ([] if local else ['grid metrics', 'grid two'])
