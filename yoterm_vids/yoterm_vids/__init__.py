"""YoTerm-Vids — play videos inside YoTerm by streaming frames as GPU-sampled
`ESC ] YT ; img` images.

The decode/schedule/resize/render pipeline lives here as a standalone package so
it can be driven two ways: as the `yoterm-vids` CLI (emits YT image sequences to
any YoTerm-capable stdout), and, later, from inside YoTerm itself behind a native
`YT;vid` sequence.
"""

__version__ = "0.1.0"
