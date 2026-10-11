/* The independent runtime follows arm64 Darwin, where abs() and labs() are
 * compiler builtins rather than libSystem exports. This layer compiles with
 * -fno-builtin like the other command layers, so the two entry points are
 * provided explicitly here. */
int
abs(int value)
{
    return value < 0 ? -value : value;
}

long
labs(long value)
{
    return value < 0 ? -value : value;
}
