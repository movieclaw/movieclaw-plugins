"""Build the native single-frame page from the standalone source. No dependencies."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def render():
    source = (ROOT / 'platform_discovery/assets/page.html').read_text()
    # All CSS and JS remain on the authenticated, whitelisted plugin asset API.
    # Absolute same-origin paths work at every native subroute, LAN and WAN.
    prefix = '/api/v1/plugins/platform-discovery/'
    import re
    return re.sub(r'((?:src|href)=")((?:assets/)?[a-z-]+\.(?:js|css|svg))"',
                  lambda match: match[1] + prefix + match[2] + '"', source)


if __name__ == '__main__':
    target = ROOT / 'web/index.html'
    result = render()
    if '--check' in sys.argv:
        if target.read_text() != result:
            raise SystemExit('Native page is stale: run python3 tools/build_web.py')
    else:
        target.write_text(result)
