from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import List

from django.conf import settings


class Tokenizer(ABC):
    """Abstract base class for text tokenization strategies."""
    
    @abstractmethod
    def tokenize(self, text: str | None) -> List[str]:
        """Tokenize text into a list of tokens.
        
        Args:
            text: Input text to tokenize, can be None
            
        Returns:
            List of token strings
        """
        pass


class NaiveTokenizer(Tokenizer):
    """Regex-based tokenizer using simple word pattern matching."""
    
    def __init__(self):
        # Word pattern: alphanumeric sequences
        self._word_re = re.compile(r"[a-z0-9]+", re.IGNORECASE)
        # Common English stopwords
        self._stopwords = {
            "the", "a", "an", "and", "or", "is", "are", "of", "to", "in", 
            "for", "on", "with", "as", "by", "at"
        }
    
    def tokenize(self, text: str | None) -> List[str]:
        """Tokenize text using regex pattern matching and stopword filtering."""
        if not text:
            return []
        
        tokens = [m.group(0).lower() for m in self._word_re.finditer(text)]
        return [t for t in tokens if t not in self._stopwords]


class NLTKTokenizer(Tokenizer):
    """NLTK-based tokenizer with stopword filtering."""
    
    def __init__(self):
        try:
            import nltk
            from nltk.corpus import stopwords
            from nltk.tokenize import word_tokenize
            
            # Download required NLTK data if not present
            try:
                nltk.data.find('tokenizers/punkt')
            except LookupError:
                nltk.download('punkt', quiet=True)
            
            try:
                nltk.data.find('corpora/stopwords')
            except LookupError:
                nltk.download('stopwords', quiet=True)
            
            self._word_tokenize = word_tokenize
            self._stopwords = set(stopwords.words('english'))
            
        except ImportError:
            raise ImportError(
                "NLTK is required for NLTKTokenizer. Install with: pip install nltk"
            )
    
    def tokenize(self, text: str | None) -> List[str]:
        """Tokenize text using NLTK word_tokenize with stopword filtering."""
        if not text:
            return []
        
        # Tokenize and filter
        tokens = self._word_tokenize(text.lower())
        # Filter tokens: alphanumeric, not stopwords, reasonable length, and not too many repeated characters
        filtered_tokens = []
        for t in tokens:
            # Skip if too long (more restrictive limit)
            if len(t) > 50:
                continue
            # Skip if all same character (like "aaaaaaa")
            if len(set(t)) <= 1:
                continue
            # Skip if it's a long number
            if t.isdigit() and len(t) > 10:
                continue
            # Skip if it's not alphanumeric
            if not t.isalnum():
                continue
            # Skip if it's a stopword
            if t in self._stopwords:
                continue
            # Skip tokens with too many repeated character patterns (like "ycdbuyfcdghuyedhuedhyuhecduohcfrufrirvfuygrvfuycrfuyecfuvrfuycrfrcubrfhugrvfuygbcrfuybcrfygfuyrvfguy")
            if len(t) > 20 and len(set(t)) < len(t) * 0.3:  # If more than 20 chars and less than 30% unique chars
                continue
            
            filtered_tokens.append(t)
        
        return filtered_tokens


class GPTTokenizer(Tokenizer):
    """GPT tokenizer using tiktoken with cl100k_base encoding."""
    
    def __init__(self):
        try:
            import tiktoken
            self._encoding = tiktoken.get_encoding("cl100k_base")
        except ImportError:
            raise ImportError(
                "tiktoken is required for GPTTokenizer. Install with: pip install tiktoken"
            )
    
    def tokenize(self, text: str | None) -> List[str]:
        """Tokenize text using GPT-4 tokenizer (tiktoken cl100k_base)."""
        if not text:
            return []
        
        # Encode to token IDs, then decode back to tokens
        # This gives us the actual token strings that the model would see
        token_ids = self._encoding.encode(text)
        tokens = [self._encoding.decode([token_id]) for token_id in token_ids]
        
        # Filter out empty tokens and whitespace-only tokens
        return [t for t in tokens if t.strip()]


def get_tokenizer() -> Tokenizer:
    """Get the configured tokenizer instance based on Django settings.
    
    Returns:
        Tokenizer instance based on TOKENIZER_TYPE setting
        
    Raises:
        ValueError: If TOKENIZER_TYPE is not recognized
    """
    tokenizer_type = getattr(settings, 'TOKENIZER_TYPE', 'gpt').lower()
    
    if tokenizer_type == 'naive':
        return NaiveTokenizer()
    elif tokenizer_type == 'nltk':
        return NLTKTokenizer()
    elif tokenizer_type == 'gpt':
        return GPTTokenizer()
    else:
        raise ValueError(
            f"Unknown TOKENIZER_TYPE: {tokenizer_type}. "
            f"Valid options: 'naive', 'nltk', 'gpt'"
        )


# Global tokenizer instance (lazy-loaded)
_tokenizer_instance: Tokenizer | None = None


def tokenize_configurable(text: str | None) -> List[str]:
    """Convenience function that uses the configured tokenizer.
    
    This maintains backward compatibility with existing code that imports
    tokenize directly from search_engine.search.
    
    Args:
        text: Input text to tokenize
        
    Returns:
        List of token strings
    """
    global _tokenizer_instance
    if _tokenizer_instance is None:
        _tokenizer_instance = get_tokenizer()
    return _tokenizer_instance.tokenize(text)


# Cache NLTK tokenizer instance
_nltk_tokenizer_instance: NLTKTokenizer | None = None


def tokenize(text: str | None) -> List[str]:
    """Tokenize for TF-IDF and search (always uses NLTK).
    
    This function is used for TF-IDF indexing and web app search functionality.
    It always uses NLTK tokenizer for consistent linguistic tokenization.
    
    Args:
        text: Input text to tokenize
        
    Returns:
        List of token strings using NLTK tokenization
    """
    global _nltk_tokenizer_instance
    if _nltk_tokenizer_instance is None:
        _nltk_tokenizer_instance = NLTKTokenizer()
    return _nltk_tokenizer_instance.tokenize(text)


# Cache GPT tokenizer instance
_gpt_tokenizer_instance: GPTTokenizer | None = None


def tokenize_gpt(text: str | None) -> List[str]:
    """Tokenize for QA dataset generation (always uses GPT).
    
    This function is used for QA dataset generation where GPT token counting
    is required for LLM compatibility.
    
    Args:
        text: Input text to tokenize
        
    Returns:
        List of token strings using GPT tokenization
    """
    global _gpt_tokenizer_instance
    if _gpt_tokenizer_instance is None:
        _gpt_tokenizer_instance = GPTTokenizer()
    return _gpt_tokenizer_instance.tokenize(text)
