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
    
    # PyTorch with ROCm support for AMD GPU acceleration
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

        ### ML
        numpy

        ### NLP
        spacy
        spacy-models.en_core_web_sm

        ### DL
        # Use the variable defined above for torch and its ecosystem
        torch
        (torchaudio.override { inherit torch; })
        (torchvision.override { inherit torch; })

        django
        psycopg
        lxml
        orjson
        tqdm
        numpy
        scipy
        tiktoken
        nltk
        psutil
    ]))
    
    # Development utilities
    git
    curl
    wget
  ];
}
