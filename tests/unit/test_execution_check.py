from forexai.cli import build_parser


def test_cli_exposes_execution_check_command() -> None:
    args = build_parser().parse_args(["execution-check"])
    assert args.command == "execution-check"


def test_cli_exposes_semi_execution_check_command() -> None:
    args = build_parser().parse_args(["semi-execution-check"])
    assert args.command == "semi-execution-check"
