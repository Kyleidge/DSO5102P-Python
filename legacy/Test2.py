#! /usr/bin/python3
# -*- coding: utf-8 -*-

import time
from rcr.dso5102p.DSO5102P import DSO5102P

def main():
    dso = DSO5102P(  0x049f, 0x505a, True )

    # RemoteShell ('ls /*.inf') was removed for safety; the file names are known.

    print( 'Getting keyprotocol.inf' )
    r = dso.ReadFile( '/keyprotocol.inf' )
    f=open( './keyprotocol.inf', 'w' )
    f.write( r )
    f.close()

    print( 'Getting protocol.inf' )
    r = dso.ReadFile( '/protocol.inf' )
    f=open( './protocol.inf', 'w' )
    f.write( r )
    f.close()

    print( 'Getting sys.inf' )
    r = dso.ReadFile( '/sys.inf' )
    f=open( './sys.inf', 'w' )
    f.write( r )
    f.close()


# show time
main()
