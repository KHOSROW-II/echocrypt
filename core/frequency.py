def validate(mark,space,rate):
    if min(mark,space)<=0 or mark==space or max(mark,space)>=rate/2: raise ValueError("Invalid FSK frequency plan")
    return True
