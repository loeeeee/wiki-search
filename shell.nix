{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    # Python 3.13 and development tools
    python313
    python313Packages.pip
    python313Packages.setuptools
    python313Packages.wheel
    
    # System libraries needed by numpy/scipy
    gcc-unwrapped
    stdenv.cc.cc.lib
    zlib
    bzip2
    libffi
    openssl
    
    # PostgreSQL client libraries for psycopg
    postgresql
    
    # Build tools for Python packages with native extensions
    gcc
    gnumake
    pkg-config
    
    # Additional libraries commonly needed by scientific Python packages
    lapack
    blas
    openblas
    gfortran
    
    # Development utilities
    git
    curl
    wget
  ];

  # Set environment variables for proper library linking
  shellHook = ''
    # Ensure system libraries are available in standard paths
    export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:${pkgs.gcc-unwrapped}/lib:${pkgs.zlib}/lib:${pkgs.bzip2}/lib:${pkgs.libffi}/lib:$LD_LIBRARY_PATH"
    
    # Set Python path to include system packages
    export PYTHONPATH="${pkgs.python313}/lib/python3.13/site-packages:$PYTHONPATH"
    
    # PostgreSQL environment
    export PG_CONFIG="${pkgs.postgresql}/bin/pg_config"
    
    # Compiler flags for building Python packages
    export CPPFLAGS="-I${pkgs.zlib.dev}/include -I${pkgs.bzip2.dev}/include"
    export LDFLAGS="-L${pkgs.zlib}/lib -L${pkgs.bzip2}/lib"
    
    echo "NixOS development environment loaded!"
    echo "Python: $(python3 --version)"
    echo "Available libraries: libstdc++, zlib, bzip2, PostgreSQL"
    echo "Ready to run: uv run python wiki_search/manage.py build_pagerank --rebuild --verbose"
  '';
}
