#!/usr/bin/env python3
"""
Release script for opamp-server-py.
Creates and pushes a new git tag for releases.
"""

import argparse
import re
import subprocess
import sys
from datetime import datetime
from typing import Optional


def run_git_command(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        check=check
    )
    return result


def get_latest_tag() -> Optional[str]:
    """Get the latest tag from the repository."""
    result = run_git_command(["describe", "--tags", "--abbrev=0"], check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def get_all_tags() -> list[str]:
    """Get all tags sorted by version."""
    result = run_git_command(["tag", "-l"], check=False)
    if result.returncode == 0:
        tags = result.stdout.strip().split("\n")
        # Filter out empty strings and sort
        tags = [t for t in tags if t]
        return sorted(tags)
    return []


def parse_version(tag: str) -> tuple[int, int, int, str]:
    """
    Parse a version tag into components.
    Returns (major, minor, patch, suffix)
    """
    # Match patterns like v1.2.3 or v1.2.3-alpha
    match = re.match(r'^v?(\d+)\.(\d+)\.(\d+)(?:-(\w+))?$', tag)
    if match:
        major, minor, patch, suffix = match.groups()
        return (int(major), int(minor), int(patch), suffix or "")
    return (0, 0, 0, "")


def bump_version(
    tag: Optional[str],
    bump_type: str = "patch"
) -> str:
    """
    Bump the version based on the latest tag.
    
    Args:
        tag: The latest tag (or None if no tags exist)
        bump_type: One of 'major', 'minor', 'patch'
    
    Returns:
        The new version string
    """
    if tag is None:
        return "v0.1.0"
    
    major, minor, patch, suffix = parse_version(tag)
    
    if bump_type == "major":
        return f"v{major + 1}.0.0"
    elif bump_type == "minor":
        return f"v{major}.{minor + 1}.0"
    else:  # patch
        return f"v{major}.{minor}.{patch + 1}"


def validate_version(version: str) -> bool:
    """Validate that a version string follows semver format."""
    pattern = r'^v\d+\.\d+\.\d+(?:-[\w\.]+)?$'
    return bool(re.match(pattern, version))


def create_tag(version: str, message: Optional[str] = None) -> bool:
    """Create an annotated git tag."""
    if message is None:
        message = f"Release {version}"
    
    result = run_git_command(
        ["tag", "-a", version, "-m", message],
        check=False
    )
    
    if result.returncode == 0:
        print(f"✓ Created tag {version}")
        return True
    else:
        print(f"✗ Failed to create tag: {result.stderr}")
        return False


def push_tag(version: str) -> bool:
    """Push the tag to the remote repository."""
    result = run_git_command(
        ["push", "origin", version],
        check=False
    )
    
    if result.returncode == 0:
        print(f"✓ Pushed tag {version} to origin")
        return True
    else:
        print(f"✗ Failed to push tag: {result.stderr}")
        return False


def get_changelog_since_tag(tag: Optional[str]) -> str:
    """Get commit messages since the last tag."""
    if tag:
        result = run_git_command(
            ["log", f"{tag}..HEAD", "--pretty=format:- %s"],
            check=False
        )
    else:
        result = run_git_command(
            ["log", "--pretty=format:- %s", "-n", "20"],
            check=False
        )
    
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


def ensure_clean_working_directory() -> bool:
    """Check if the working directory is clean."""
    result = run_git_command(["status", "--porcelain"], check=False)
    if result.stdout.strip():
        print("⚠ Working directory is not clean. Uncommitted changes:")
        print(result.stdout)
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Create and push a new release tag"
    )
    parser.add_argument(
        "--version", "-v",
        help="Specify the version to release (e.g., v1.2.3)"
    )
    parser.add_argument(
        "--bump", "-b",
        choices=["major", "minor", "patch"],
        default="patch",
        help="Version bump type (default: patch)"
    )
    parser.add_argument(
        "--message", "-m",
        help="Tag message (default: 'Release {version}')"
    )
    parser.add_argument(
        "--no-push", "-n",
        action="store_true",
        help="Create tag but don't push to remote"
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Force create tag even if working directory is not clean"
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List all existing tags and exit"
    )
    
    args = parser.parse_args()
    
    # List tags if requested
    if args.list:
        tags = get_all_tags()
        if tags:
            print("Existing tags:")
            for tag in tags:
                print(f"  {tag}")
        else:
            print("No tags found")
        return 0
    
    # Check working directory
    if not args.force and not ensure_clean_working_directory():
        print("\nUse --force to create tag anyway, or commit your changes first.")
        return 1
    
    # Get current branch
    branch_result = run_git_command(["branch", "--show-current"], check=False)
    current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"
    print(f"Current branch: {current_branch}")
    
    # Get latest tag
    latest_tag = get_latest_tag()
    if latest_tag:
        print(f"Latest tag: {latest_tag}")
    else:
        print("No existing tags found")
    
    # Determine version
    if args.version:
        new_version = args.version
        if not validate_version(new_version):
            print(f"Invalid version format: {new_version}")
            print("Version should follow semantic versioning (e.g., v1.2.3)")
            return 1
    else:
        new_version = bump_version(latest_tag, args.bump)
        print(f"Suggested version: {new_version}")
        response = input("Proceed with this version? [Y/n]: ").strip().lower()
        if response and response not in ('y', 'yes'):
            print("Aborted")
            return 0
    
    # Check if tag already exists
    existing_tags = get_all_tags()
    if new_version in existing_tags:
        print(f"Tag {new_version} already exists!")
        return 1
    
    # Show changelog
    print("\nChanges since last tag:")
    changelog = get_changelog_since_tag(latest_tag)
    if changelog:
        print(changelog)
    else:
        print("(no commits since last tag)")
    
    # Confirm
    print(f"\nReady to create tag: {new_version}")
    if not args.no_push:
        print("This will push the tag to origin")
    
    response = input("\nContinue? [y/N]: ").strip().lower()
    if response not in ('y', 'yes'):
        print("Aborted")
        return 0
    
    # Create tag
    if not create_tag(new_version, args.message):
        return 1
    
    # Push tag
    if not args.no_push:
        if not push_tag(new_version):
            print(f"\nTag was created locally but failed to push.")
            print(f"You can retry with: git push origin {new_version}")
            return 1
    
    print(f"\n✓ Release {new_version} {'created' if args.no_push else 'completed'}!")
    
    if not args.no_push:
        print(f"\nView the tag at:")
        # Try to get remote URL
        remote_result = run_git_command(
            ["remote", "get-url", "origin"],
            check=False
        )
        if remote_result.returncode == 0:
            remote_url = remote_result.stdout.strip()
            # Convert SSH URL to HTTPS for browsing
            if remote_url.startswith("git@github.com:"):
                repo_path = remote_url.replace("git@github.com:", "").replace(".git", "")
                print(f"  https://github.com/{repo_path}/releases/tag/{new_version}")
            elif "github.com" in remote_url:
                repo_path = remote_url.replace("https://github.com/", "").replace(".git", "")
                print(f"  https://github.com/{repo_path}/releases/tag/{new_version}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
