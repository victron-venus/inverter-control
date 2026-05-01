# Contributing to Inverter Control

Thank you for your interest in contributing!

## How to Contribute

### Reporting Bugs

1. Check existing [issues](https://github.com/victron-venus/inverter-control/issues) to avoid duplicates
2. Use the bug report template
3. Include:
   - Venus OS version
   - Hardware setup (Cerbo GX, MultiPlus model, etc.)
   - Steps to reproduce
   - Expected vs actual behavior
   - Relevant logs

### Suggesting Features

1. Open a feature request issue
2. Describe the use case
3. Explain why it would benefit others

### Pull Requests

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make your changes
4. Test on actual Venus OS hardware if possible
5. Run linter: `ruff check .`
6. Use commit.sh script to create PR:
   ```bash
   echo "Add feature: your description" > commit.txt
   echo "" >> commit.txt
   echo "Detailed changelog here" >> commit.txt
   ./commit.sh
   ```
7. The script creates PR with auto-merge label
8. After CI checks pass and approval, PR auto-merges

**Note:** Direct push to main is prevented. Always use feature branch + PR.

### Code Style

- Follow PEP 8
- Use meaningful variable names
- Add comments for complex logic
- Keep functions focused and small

### Testing

- Test on Venus OS (Cerbo GX, Raspberry Pi, etc.)
- Verify D-Bus communication works
- Check web interface functionality
- Test with DRY_RUN=True first

## Development Setup

```bash
# Clone
git clone https://github.com/victron-venus/inverter-control.git
cd inverter-control

# Create secrets.py from example
cp secrets.example.py secrets.py
# Edit secrets.py with your values

# Run locally (dry run mode)
python3 main.py --dry-run
```

## Questions?

- Open a [Discussion](https://github.com/victron-venus/inverter-control/discussions)
- Ask on [Victron Community](https://community.victronenergy.com/)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
