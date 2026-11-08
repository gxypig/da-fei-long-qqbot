"""Encode actual generated action frames; never invent motion by panning a still."""
import io,hashlib
from PIL import Image

def frames_to_gif(raw):
    with Image.open(io.BytesIO(raw)) as sheet:
        sheet.load()
        if sheet.size!=(1536,1024):raise ValueError('animation_grid_dimensions')
        frames=[sheet.crop((x*512,y*512,(x+1)*512,(y+1)*512)).convert('RGB') for y in range(2) for x in range(3)]
    try:
        if len({hashlib.sha256(f.tobytes()).digest() for f in frames})<3:raise ValueError('animation_frames_not_distinct')
        out=io.BytesIO();frames[0].save(out,'GIF',save_all=True,append_images=frames[1:],duration=200,loop=0,disposal=2,optimize=False)
        result=out.getvalue()
        with Image.open(io.BytesIO(result)) as animation:
            if getattr(animation,'n_frames',1)!=6:raise ValueError('animation_encoding_lost_frames')
        return result
    finally:
        for frame in frames:frame.close()

def sample_sheet(raw):
    with Image.open(io.BytesIO(raw)) as picture:
        count=getattr(picture,'n_frames',1)
        if count<2:return raw
        sheet=Image.new('RGB',(1536,1024),'white')
        for i in range(6):
            picture.seek(round(i*(count-1)/5));frame=picture.convert('RGB');frame.thumbnail((512,512))
            sheet.paste(frame,((i%3)*512+(512-frame.width)//2,(i//3)*512+(512-frame.height)//2));frame.close()
        out=io.BytesIO();sheet.save(out,'PNG');sheet.close();return out.getvalue()
