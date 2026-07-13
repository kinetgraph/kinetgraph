from pathlib import Path
import typer
from rich.console import Console
from kntgraph.security.keys._generate import generate_keypair
from cryptography.hazmat.primitives import serialization

app = typer.Typer(help="Manage Level 1 cryptographic keys for Kinetgraph Agents.")
console = Console()


@app.command()
def generate(
    agent_id: str = typer.Option(
        ..., "--agent-id", help="The ID of the Agent for which to generate the keys."
    ),
    out_dir: str = typer.Option(
        None,
        "--out-dir",
        help="Directory to save the PEM files. If empty, prints to stdout.",
    ),
):
    """
    Generate an Ed25519 keypair for an Agent.
    """
    priv_wrapper, pub_wrapper = generate_keypair()

    # ``generate_keypair`` returns the Ed25519 wrappers
    # (real keypair, not a stub — stubs are produced by
    # ``generate_stub_keypair``). The wrapper exposes
    # ``.bytes``/``.public_key()`` via duck typing;
    # the PEM conversion reads the underlying
    # ``cryptography`` object via ``.private_bytes`` /
    # ``.public_bytes``.
    private_pem = priv_wrapper._key.private_bytes(  # type: ignore[attr-defined]
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    public_pem = pub_wrapper._key.public_bytes(  # type: ignore[attr-defined]
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    if out_dir:
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        priv_file = out_path / f"{agent_id}_private.pem"
        pub_file = out_path / f"{agent_id}_public.pem"

        priv_file.write_bytes(private_pem)
        pub_file.write_bytes(public_pem)

        console.print(
            f"[green]Success![/green] Keys generated for agent '{agent_id}' at {out_path}"
        )
    else:
        # Print directly to stdout without rich formatting so it can be piped safely
        print(private_pem.decode("utf-8"))
        print(public_pem.decode("utf-8"))
