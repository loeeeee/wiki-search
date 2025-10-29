{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
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
    
    ## Python tools
    (python313.withPackages (python-pkgs: with python-pkgs; [
        pip
        requests
        setuptools
        wheel

        ### Observerbility
        tqdm

        ### ML
        numpy
        scipy

        ## Tools
        lxml
        orjson
        psutil
        psycopg
        django

        ## NLP
        nltk
        tiktoken
      ]))
    
    # Development utilities
    git
    curl
    wget
  ];
}
