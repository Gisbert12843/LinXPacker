#!/usr/bin/env python3
"""
Package builder for pacman (Arch Linux) and dpkg (Debian/Ubuntu).
Reads package information from 'package_control' file and recursively
packages all files from the 'usr' directory.
"""

import os
import sys
import shutil
import subprocess
import tarfile
import hashlib
import tempfile
import argparse
from pathlib import Path
from datetime import datetime


class PackageInfo:
    """Parse and store package information from package_control file."""
    
    def __init__(self, control_file: str):
        self.package = ""
        self.version = ""
        self.architecture = "all"
        self.maintainer = ""
        self.description = ""
        self.depends = []
        self.url = ""
        self.license = "custom"
        
        self._parse_control(control_file)
    
    def _parse_control(self, control_file: str):
        """Parse the package_control file."""
        if not os.path.exists(control_file):
            raise FileNotFoundError(f"Control file not found: {control_file}")
        
        with open(control_file, 'r') as f:
            content = f.read()
        
        current_key = None
        current_value = []
        
        for line in content.split('\n'):
            if line.startswith(' ') or line.startswith('\t'):
                # Continuation of previous field
                if current_key:
                    current_value.append(line.strip())
            elif ':' in line:
                # Save previous key-value pair
                if current_key:
                    self._set_field(current_key, ' '.join(current_value))
                
                # Parse new key-value pair
                key, value = line.split(':', 1)
                current_key = key.strip()
                current_value = [value.strip()]
            else:
                # Empty line or other - save current if exists
                if current_key:
                    self._set_field(current_key, ' '.join(current_value))
                    current_key = None
                    current_value = []
        
        # Don't forget the last field
        if current_key:
            self._set_field(current_key, ' '.join(current_value))
    
    def _set_field(self, key: str, value: str):
        """Set a field based on key name."""
        key_lower = key.lower()
        if key_lower == 'package':
            self.package = value
        elif key_lower == 'version':
            self.version = value
        elif key_lower == 'architecture':
            self.architecture = value
        elif key_lower == 'maintainer':
            self.maintainer = value
        elif key_lower == 'description':
            self.description = value
        elif key_lower == 'depends':
            # Parse comma-separated dependencies
            self.depends = [d.strip() for d in value.split(',') if d.strip()]
        elif key_lower == 'url':
            self.url = value
        elif key_lower == 'license':
            self.license = value
    
    def get_pacman_arch(self) -> str:
        """Convert architecture to pacman format."""
        arch_map = {
            'all': 'any',
            'amd64': 'x86_64',
            'i386': 'i686',
            'arm64': 'aarch64',
            'armhf': 'armv7h',
        }
        return arch_map.get(self.architecture, self.architecture)
    
    def get_deb_arch(self) -> str:
        """Return architecture in Debian format."""
        return self.architecture


def get_all_files(source_dir: str) -> list:
    """Recursively get all files in a directory."""
    files = []
    for root, dirs, filenames in os.walk(source_dir):
        for filename in filenames:
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, source_dir)
            files.append((full_path, rel_path))
    return files


def calculate_installed_size(source_dir: str) -> int:
    """Calculate total installed size in KB."""
    total_size = 0
    for root, dirs, files in os.walk(source_dir):
        for f in files:
            total_size += os.path.getsize(os.path.join(root, f))
    return (total_size + 1023) // 1024  # Round up to KB


def build_deb_package(pkg_info: PackageInfo, source_dir: str, output_dir: str) -> str:
    """Build a .deb package."""
    print(f"Building .deb package...")
    
    # Create temporary build directory
    with tempfile.TemporaryDirectory() as build_dir:
        pkg_name = f"{pkg_info.package}_{pkg_info.version}_{pkg_info.get_deb_arch()}"
        pkg_dir = os.path.join(build_dir, pkg_name)
        
        # Copy usr directory contents
        usr_dest = os.path.join(pkg_dir, 'usr')
        shutil.copytree(source_dir, usr_dest)
        
        # Create DEBIAN directory
        debian_dir = os.path.join(pkg_dir, 'DEBIAN')
        os.makedirs(debian_dir, exist_ok=True)
        
        # Calculate installed size
        installed_size = calculate_installed_size(usr_dest)
        
        # Create control file
        control_content = f"""Package: {pkg_info.package}
Version: {pkg_info.version}
Architecture: {pkg_info.get_deb_arch()}
Maintainer: {pkg_info.maintainer}
Installed-Size: {installed_size}
"""
        if pkg_info.depends:
            control_content += f"Depends: {', '.join(pkg_info.depends)}\n"
        
        control_content += f"Description: {pkg_info.description}\n"
        
        with open(os.path.join(debian_dir, 'control'), 'w') as f:
            f.write(control_content)
        
        # Create md5sums file
        md5sums = []
        for full_path, rel_path in get_all_files(usr_dest):
            with open(full_path, 'rb') as f:
                md5 = hashlib.md5(f.read()).hexdigest()
            # Path in md5sums is relative to package root
            md5sums.append(f"{md5}  usr/{rel_path}")
        
        with open(os.path.join(debian_dir, 'md5sums'), 'w') as f:
            f.write('\n'.join(md5sums) + '\n')
        
        # Set proper permissions
        for full_path, rel_path in get_all_files(usr_dest):
            if '/bin/' in full_path or full_path.endswith('/bin'):
                os.chmod(full_path, 0o755)
            else:
                os.chmod(full_path, 0o644)
        
        # Create the .deb package
        output_file = os.path.join(output_dir, f"{pkg_name}.deb")
        
        # Try dpkg-deb first, fall back to manual creation
        try:
            subprocess.run(
                ['dpkg-deb', '--build', '--root-owner-group', pkg_dir, output_file],
                check=True,
                capture_output=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Manual .deb creation using ar and tar
            print("dpkg-deb not found, creating .deb manually...")
            output_file = create_deb_manually(pkg_dir, output_dir, pkg_name)
        
        print(f"Created: {output_file}")
        return output_file


def create_deb_manually(pkg_dir: str, output_dir: str, pkg_name: str) -> str:
    """Create a .deb package manually without dpkg-deb."""
    output_file = os.path.join(output_dir, f"{pkg_name}.deb")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create debian-binary
        with open(os.path.join(temp_dir, 'debian-binary'), 'w') as f:
            f.write('2.0\n')
        
        # Create control.tar.gz
        control_tar = os.path.join(temp_dir, 'control.tar.gz')
        with tarfile.open(control_tar, 'w:gz') as tar:
            debian_dir = os.path.join(pkg_dir, 'DEBIAN')
            for f in os.listdir(debian_dir):
                tar.add(os.path.join(debian_dir, f), arcname=f)
        
        # Create data.tar.gz
        data_tar = os.path.join(temp_dir, 'data.tar.gz')
        with tarfile.open(data_tar, 'w:gz') as tar:
            usr_dir = os.path.join(pkg_dir, 'usr')
            tar.add(usr_dir, arcname='usr')
        
        # Create .deb using ar
        try:
            subprocess.run(
                ['ar', 'rcs', output_file,
                 os.path.join(temp_dir, 'debian-binary'),
                 control_tar,
                 data_tar],
                check=True,
                capture_output=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise RuntimeError(f"Failed to create .deb package: {e}")
    
    return output_file


def get_all_dirs(source_dir: str) -> list:
    """Recursively get all directories in a directory."""
    dirs = []
    for root, dirnames, filenames in os.walk(source_dir):
        for dirname in dirnames:
            full_path = os.path.join(root, dirname)
            rel_path = os.path.relpath(full_path, source_dir)
            dirs.append((full_path, rel_path))
    return dirs


def build_pacman_package(pkg_info: PackageInfo, source_dir: str, output_dir: str) -> str:
    """Build a pacman .pkg.tar.zst package."""
    print(f"Building pacman package...")
    
    with tempfile.TemporaryDirectory() as build_dir:
        pkg_name = f"{pkg_info.package}-{pkg_info.version}-1-{pkg_info.get_pacman_arch()}"
        pkg_dir = os.path.join(build_dir, pkg_name)
        
        # Copy usr directory contents
        usr_dest = os.path.join(pkg_dir, 'usr')
        shutil.copytree(source_dir, usr_dest)
        
        # Set proper permissions
        for full_path, rel_path in get_all_files(usr_dest):
            if '/bin/' in full_path:
                os.chmod(full_path, 0o755)
            else:
                os.chmod(full_path, 0o644)
        
        # Calculate installed size
        installed_size = calculate_installed_size(usr_dest)
        
        # Create .PKGINFO file
        builddate = int(datetime.now().timestamp())
        
        pkginfo_content = f"""# Generated by pack.py
pkgname = {pkg_info.package}
pkgbase = {pkg_info.package}
pkgver = {pkg_info.version}-1
pkgdesc = {pkg_info.description}
url = {pkg_info.url if pkg_info.url else ''}
builddate = {builddate}
packager = {pkg_info.maintainer}
size = {installed_size * 1024}
arch = {pkg_info.get_pacman_arch()}
license = {pkg_info.license}
"""
        # Add dependencies
        for dep in pkg_info.depends:
            # Convert common Debian package names to Arch equivalents
            dep_name = convert_dep_to_pacman(dep)
            pkginfo_content += f"depend = {dep_name}\n"
        
        with open(os.path.join(pkg_dir, '.PKGINFO'), 'w') as f:
            f.write(pkginfo_content)
        
        # Create .MTREE content with proper format
        mtree_lines = ["#mtree"]
        
        # Add .PKGINFO entry
        pkginfo_md5 = hashlib.md5(pkginfo_content.encode()).hexdigest()
        pkginfo_sha256 = hashlib.sha256(pkginfo_content.encode()).hexdigest()
        mtree_lines.append(f"./.PKGINFO time={builddate}.0 size={len(pkginfo_content)} md5digest={pkginfo_md5} sha256digest={pkginfo_sha256}")
        
        # Add usr directory entry
        mtree_lines.append(f"./usr time={builddate}.0 type=dir")
        
        # Add all subdirectories
        for full_path, rel_path in get_all_dirs(usr_dest):
            mtree_lines.append(f"./usr/{rel_path} time={builddate}.0 type=dir")
        
        # Add all files
        for full_path, rel_path in get_all_files(usr_dest):
            stat_info = os.stat(full_path)
            mode = oct(stat_info.st_mode)[-3:]
            size = stat_info.st_size
            
            with open(full_path, 'rb') as f:
                content = f.read()
                md5 = hashlib.md5(content).hexdigest()
                sha256 = hashlib.sha256(content).hexdigest()
            
            mtree_lines.append(f"./usr/{rel_path} time={builddate}.0 size={size} mode={mode} type=file md5digest={md5} sha256digest={sha256}")
        
        mtree_content = '\n'.join(mtree_lines) + '\n'
        
        # Write .MTREE as gzip compressed
        import gzip
        mtree_path = os.path.join(pkg_dir, '.MTREE')
        with gzip.open(mtree_path, 'wt', encoding='utf-8') as f:
            f.write(mtree_content)
        
        # Create the package archive
        output_file = os.path.join(output_dir, f"{pkg_name}.pkg.tar.zst")
        
        # Remove existing output file to avoid zstd prompts
        if os.path.exists(output_file):
            os.remove(output_file)
        
        # Try zstd compression first, fall back to gzip
        try:
            # Create tar and compress with zstd
            tar_file = os.path.join(build_dir, f"{pkg_name}.tar")
            with tarfile.open(tar_file, 'w') as tar:
                # Add metadata files first
                tar.add(os.path.join(pkg_dir, '.PKGINFO'), arcname='.PKGINFO')
                tar.add(mtree_path, arcname='.MTREE')
                # Add usr directory
                tar.add(usr_dest, arcname='usr')
            
            # Compress with zstd (level 3 is fast and good enough)
            subprocess.run(
                ['zstd', '-3', '-f', tar_file, '-o', output_file],
                check=True,
                capture_output=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Fall back to gzip
            print("zstd not found, falling back to gzip compression...")
            output_file = os.path.join(output_dir, f"{pkg_name}.pkg.tar.gz")
            with tarfile.open(output_file, 'w:gz') as tar:
                tar.add(os.path.join(pkg_dir, '.PKGINFO'), arcname='.PKGINFO')
                tar.add(mtree_path, arcname='.MTREE')
                tar.add(usr_dest, arcname='usr')
        
        print(f"Created: {output_file}")
        return output_file


def convert_dep_to_pacman(dep: str) -> str:
    """Convert Debian package names to Arch Linux equivalents."""
    # Common conversions
    conversions = {
        'python3': 'python',
        'python3-dev': 'python',
        'build-essential': 'base-devel',
        'libssl-dev': 'openssl',
        'libcurl4-openssl-dev': 'curl',
    }
    
    # Remove version constraints for now (e.g., "python3 (>= 3.6)")
    dep_name = dep.split('(')[0].strip()
    
    return conversions.get(dep_name, dep_name)


def main():
    parser = argparse.ArgumentParser(
        description='Build packages for pacman and dpkg from usr directory',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Build both packages
  %(prog)s --deb              # Build only .deb package
  %(prog)s --pacman           # Build only pacman package
  %(prog)s -o ./packages      # Output to specific directory
        """
    )
    
    parser.add_argument(
        '-c', '--control',
        default='package_control',
        help='Path to package control file (default: package_control)'
    )
    parser.add_argument(
        '-s', '--source',
        default='usr',
        help='Source directory to package (default: usr)'
    )
    parser.add_argument(
        '-o', '--output',
        default='.',
        help='Output directory for packages (default: current directory)'
    )
    parser.add_argument(
        '--deb',
        action='store_true',
        help='Build only .deb package'
    )
    parser.add_argument(
        '--pacman',
        action='store_true',
        help='Build only pacman package'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Verbose output'
    )
    
    args = parser.parse_args()
    
    # Get script directory for relative paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Resolve paths relative to script directory if not absolute
    control_file = args.control if os.path.isabs(args.control) else os.path.join(script_dir, args.control)
    source_dir = args.source if os.path.isabs(args.source) else os.path.join(script_dir, args.source)
    output_dir = args.output if os.path.isabs(args.output) else os.path.join(script_dir, args.output)
    
    # Validate paths
    if not os.path.exists(control_file):
        print(f"Error: Control file not found: {control_file}", file=sys.stderr)
        sys.exit(1)
    
    if not os.path.isdir(source_dir):
        print(f"Error: Source directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)
    
    # Create output directory if needed
    os.makedirs(output_dir, exist_ok=True)
    
    # Parse package info
    try:
        pkg_info = PackageInfo(control_file)
    except Exception as e:
        print(f"Error parsing control file: {e}", file=sys.stderr)
        sys.exit(1)
    
    if args.verbose:
        print(f"Package: {pkg_info.package}")
        print(f"Version: {pkg_info.version}")
        print(f"Architecture: {pkg_info.architecture}")
        print(f"Dependencies: {', '.join(pkg_info.depends)}")
        print()
    
    # Determine which packages to build
    build_deb = args.deb or (not args.deb and not args.pacman)
    build_pac = args.pacman or (not args.deb and not args.pacman)
    
    results = []
    
    try:
        if build_deb:
            deb_file = build_deb_package(pkg_info, source_dir, output_dir)
            results.append(('deb', deb_file))
        
        if build_pac:
            pac_file = build_pacman_package(pkg_info, source_dir, output_dir)
            results.append(('pacman', pac_file))
        
        print("\n" + "=" * 50)
        print("Build complete!")
        print("=" * 50)
        for pkg_type, path in results:
            print(f"  {pkg_type}: {path}")
        
    except Exception as e:
        print(f"Error building package: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
