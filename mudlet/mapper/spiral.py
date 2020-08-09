#                                                                        
#                                                                        
# We want to generate coordinates for this set of numbers, assuming zero in
# the origin:
#                                                                        
#                                    16                                  
#                                 17  7 15                               
#                              18  8  2  6 14                            
#                           19  9  3  0  1  5  13                        
#                              20 10  4 12 24                            
#                                 21 11 23                               
#                                    22                                  
#                                                                        
# Think of it as a circle with Manhattan measure, i.e. d(x,y) = x+y.
# A real circle has √(x²+y²) which doesn't work well on an integer grid,
# while a square has max(x,y) which isn't optimal for distributing things
# on maps.

from math import ceil as _ceil

def _max_at(r):
    return 2*r*(r+1)

# v=2*n*(n+1)
# v/2=n*(n+1)
# v/2=n*n+n
# n*n+n-v/2=0
#
# n = (-b +- sq(b*b-4*a*c))/2*a
# n = (-1 +- sq(1+4*v/2))/2
def _r_for(n):
    # Given N, return the radius of the circle required to hold it
    return _ceil((-1 + sq(1+4*n/2))/2)

def spiral_offset(n):
    """
    Given a non-negative integer N, return its x-y coordinates in a
    spiral with Manhattan distance measure.
    """
    if n == 0:
        return 0,0
    r = _r_for(n)  # radius
    b = _max_at(r-1)+1  # base = first number in a circle

    if n-b < r:  # first quadrant
        return (r-(n-b),n-b)
    n -= r
    if n-b < r:  # second quadrant
        return (-(n-b),r-(n-b))
    n -= r
    if n-b < r:  # third
        return ((n-b)-r,-(n-b))
    n -= r
    assert n-b < r  # fourth
    return ((n-b),n-b-r)

def spiral_to(r):
    """
    Generate the sequence of integer x-y coordinates representing a
    spiral (with Manhattan distance measure) up to (and including) radius r.
    """
    # We don't use `spiral_offset` because walking it manually is much more efficient.
    yield (0,0)
    n = 1
    while n <= r:
        x,y = n,0
        while x:
            yield (x,y)
            x -= 1; y += 1
        while y:
            yield (x,y)
            x -= 1; y -= 1
        while x:
            yield (x,y)
            x += 1; y -= 1
        while y:
            yield (x,y)
            x += 1; y += 1
        n += 1

