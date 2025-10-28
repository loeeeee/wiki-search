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

    ## Python tools
    (python313.withPackages (python-pkgs:
      # Define a variable for the specific torch version you want
      let
        torch = python-pkgs.torchWithRocm;
      in with python-pkgs; [
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
  ];
}
