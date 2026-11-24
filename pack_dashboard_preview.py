"""Losslessly package a complete GEO dashboard for size-limited HTML previews."""
from base64 import b64encode
import gzip
from pathlib import Path
import argparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source, output = args.source, args.output
    if source.resolve() == output.resolve():
        parser.error("请使用不同的输入和输出路径")
    data = source.read_bytes()
    compressed = gzip.compress(data, compresslevel=9, mtime=0)
    if gzip.decompress(compressed) != data:
        raise ValueError("看板压缩回读不一致")
    payload = b64encode(compressed).decode('ascii')
    output.write_text('''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GEO 看板</title><body><p id="status" role="status">正在打开 GEO 看板…</p>
<script type="module">
(async () => {
  try {
    const binary = atob(document.getElementById("compressed-board").textContent);
    const bytes = Uint8Array.from(binary, char => char.charCodeAt(0));
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
    const original = await new Response(stream).text();
    document.open();
    document.write(original);
    document.close();
  } catch (error) {
    document.getElementById("status").textContent = "当前浏览器无法解压看板。请在新版 Chrome 或 Edge 中打开。";
    console.error(error);
  }
})();
</script>
<script type="application/octet-stream" id="compressed-board">''' + payload + '''</script>
</body></html>''', encoding='utf-8')


if __name__ == "__main__":
    main()
