import pytest
from lfx.components.files_and_knowledge.deployment_file_input import DeploymentFileInputComponent


def test_deployment_file_input_builds_runtime_descriptor():
    component = DeploymentFileInputComponent(
        file_path="/runtime/documents/example.pdf",
        document_id="doc-1",
        checksum="a" * 64,
        mime_type="application/pdf",
        metadata={"source": "admin"},
    )

    result = component.build_document()

    assert result.data == {
        "file_path": "/runtime/documents/example.pdf",
        "document_id": "doc-1",
        "checksum": f"sha256:{'a' * 64}",
        "mime_type": "application/pdf",
        "metadata": {"source": "admin"},
    }


@pytest.mark.parametrize(
    ("file_path", "checksum", "error"),
    [
        ("relative.pdf", "a" * 64, "path must be absolute"),
        ("/file.pdf", "bad", "checksum must be a SHA-256"),
    ],
)
def test_deployment_file_input_rejects_invalid_runtime_descriptor(file_path, checksum, error):
    component = DeploymentFileInputComponent(
        file_path=file_path,
        document_id="doc-1",
        checksum=checksum,
        mime_type="application/pdf",
    )

    with pytest.raises(ValueError, match=error):
        component.build_document()
