from koi.plugins.harness import HarnessPlugin
from koi.protocol.stdio import serve_stdio

serve_stdio(HarnessPlugin())
