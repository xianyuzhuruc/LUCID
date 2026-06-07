import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app
from core.hub import nodes


class FilePreviewTest(unittest.TestCase):
    def test_local_raw_uses_browser_preview_mime_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            png = base / "preview.png"
            pdf = base / "preview.pdf"
            png.write_bytes(b"\x89PNG\r\n\x1a\n")
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

            self.assertEqual(app._local_file_raw(str(png)), (png.read_bytes(), "image/png"))
            self.assertEqual(app._local_file_raw(str(pdf)), (pdf.read_bytes(), "application/pdf"))

    def test_inline_headers_are_browser_preview_friendly(self) -> None:
        headers = app._inline_file_headers("/tmp/测试 文档.pdf")

        self.assertIn('Content-Disposition', headers)
        self.assertIn("inline", headers["Content-Disposition"])
        self.assertIn("filename=", headers["Content-Disposition"])
        self.assertIn("filename*=UTF-8''", headers["Content-Disposition"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_remote_raw_forwarding_uses_existing_raw_http_helper(self) -> None:
        node = nodes.NodeConfig(id="remote", kind="ssh", url="http://example.invalid")
        with mock.patch.object(nodes, "node_by_id", return_value=node), \
                mock.patch.object(nodes, "http_raw", return_value=(b"%PDF", "application/pdf")) as http_raw:
            body, mime = nodes.forward_raw("remote", "GET", "/agent/v1/files/raw?path=/tmp/a.pdf")

        self.assertEqual((body, mime), (b"%PDF", "application/pdf"))
        http_raw.assert_called_once_with(node, "GET", "/agent/v1/files/raw?path=/tmp/a.pdf", timeout_ms=10000)

    def test_frontend_sets_viewer_state_for_images_and_pdfs(self) -> None:
        html = Path("static/index.html").read_text(encoding="utf-8")

        self.assertIn("this.editor.type = isImage ? 'image' : 'pdf';", html)
        self.assertIn("this.editor.rawUrl = rawUrl;", html)
        self.assertIn("editor.type === 'image' ? editor.rawUrl : ''", html)
        self.assertIn("editor.type === 'pdf' ? editor.rawUrl : ''", html)
        self.assertIn("openRawEditorFile()", html)
        self.assertIn("window.open(this.editor.rawUrl, '_blank', 'noopener');", html)

    def test_frontend_marks_previewable_file_badges(self) -> None:
        html = Path("static/index.html").read_text(encoding="utf-8")

        self.assertIn("pdf: 'PDF'", html)
        self.assertIn("pdf: 'bg-red-50 text-red-700'", html)
        self.assertIn("png: 'IMG'", html)
        self.assertIn("gif: 'GIF'", html)
        self.assertIn("svg: 'SVG'", html)

    def test_frontend_caches_editor_state_per_terminal(self) -> None:
        html = Path("static/index.html").read_text(encoding="utf-8")

        self.assertIn("terminalEditorCache: {}", html)
        self.assertIn("saveCurrentTerminalEditorCache();", html)
        self.assertIn("restoreTerminalEditorCache(w)", html)
        self.assertIn("clearTerminalEditorCache(key)", html)
        self.assertIn("closeTerminal({ discardCache: true });", html)


if __name__ == "__main__":
    unittest.main()
