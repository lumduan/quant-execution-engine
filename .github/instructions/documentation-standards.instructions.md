---
applyTo: '**'
---
# Documentation Standards

## Purpose
Ensures comprehensive documentation standards and maintains consistency across the project.

## Responsibilities

### Docstring Standards
- Validate docstring format and completeness for all public functions
- Ensure parameter descriptions include types and constraints
- Add usage examples for complex functions
- Document exceptions and error conditions
- Maintain consistency in documentation style across modules

### API Documentation
- Create and maintain comprehensive API documentation
- Ensure all public interfaces are properly documented
- Validate code examples in documentation
- Review and improve existing documentation for clarity

### Documentation Quality
- Verify documentation accuracy and completeness
- Ensure documentation reflects current implementation
- Check for outdated or deprecated information
- Validate cross-references and links

### Example Creation
- Create realistic usage examples for complex functions
- Validate that examples work with current API
- Ensure examples follow best practices
- Document edge cases and error scenarios

## Documentation Standards

### Docstring Format
```python
async def example_function(
    param1: str,
    param2: bool = False,
) -> bool:
    """Brief description of what the function does.

    More detailed explanation if needed. Include any important
    behavioral notes or limitations.

    Args:
        param1: Description of parameter with type info.
        param2: Optional parameter description with default behavior.

    Returns:
        Description of return value and its meaning.

    Raises:
        ValueError: When parameter validation fails.
        RuntimeError: When an unexpected runtime condition occurs.

    Example:
        >>> result = await example_function("test")
        >>> print(result)
        True
    """
```

### Documentation Requirements
- ALL public functions MUST have comprehensive docstrings
- Include parameter descriptions with types and constraints
- Include return value descriptions with expected types
- Include usage examples for complex functions
- Include exception documentation with conditions
- Use consistent terminology throughout
- Provide realistic examples that can be executed
