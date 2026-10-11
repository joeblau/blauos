/* osdef.h for the blauOS Vim build, replacing the osdef.sh output.
 *
 * The SDK headers declare every other used function through the HAVE_*
 * header set in auto/config.h, so osdef.sh would strip every homebrew
 * prototype except the termcap entry points: the independent runtime has no
 * termcap, terminfo or curses library to declare them. Vim's own termlib.c
 * provides the definitions; these declarations match osdef2.h.in, with the
 * tputs() output callback typed as term.c casts it (see
 * vm/commands/vim/termlib-tputs.patch, which aligns termlib.c itself). */
extern int	tgetent(char *, char *);
extern int	tgetnum(char *);
extern int	tgetflag(char *);
extern char	*tgoto(char *, int, int);
extern int	tputs(char *, int, int (*)(int));
