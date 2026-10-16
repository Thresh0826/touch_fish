// Offline, bounded normalized-correlation search of the approved toolbar only.
// No UI operations in this class; the bridge validates and clicks separately.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

public static class WdmsToolbarIconMatcher {
    public const string Revision = "toolbar-icon-v2";
    public sealed class Match {
        public int X, Y, Width, Height, ScaleHeight;
        public double Score, RunnerUp;
        public long ElapsedMs;
    }
    sealed class Samples {
        public int[] X, Y;
        public double[] Centered;
        public double Norm;
    }
    static double[] Gray(Bitmap image) {
        Rectangle rect = new Rectangle(0,0,image.Width,image.Height);
        BitmapData data = image.LockBits(rect,ImageLockMode.ReadOnly,PixelFormat.Format32bppArgb);
        try {
            double[] gray = new double[image.Width*image.Height];
            byte[] row = new byte[image.Width*4];
            for (int y=0;y<image.Height;y++) {
                Marshal.Copy(IntPtr.Add(data.Scan0,y*data.Stride),row,0,row.Length);
                for (int x=0;x<image.Width;x++) {
                    int i=x*4;
                    gray[y*image.Width+x]=.299*row[i+2]+.587*row[i+1]+.114*row[i];
                }
            }
            return gray;
        } finally { image.UnlockBits(data); }
    }
    static Samples Sample(double[] gray,int width,int height,int step) {
        List<int> xs=new List<int>(),ys=new List<int>();
        List<double> values=new List<double>();
        double sum=0;
        for(int y=0;y<height;y+=step) for(int x=0;x<width;x+=step) {
            xs.Add(x);ys.Add(y);double value=gray[y*width+x];values.Add(value);sum+=value;
        }
        double mean=sum/values.Count, square=0;
        double[] centered=values.ToArray();
        for(int i=0;i<centered.Length;i++) { centered[i]-=mean;square+=centered[i]*centered[i]; }
        return new Samples { X=xs.ToArray(),Y=ys.ToArray(),Centered=centered,Norm=Math.Sqrt(square) };
    }
    static double Score(double[] image,int stride,int x,int y,Samples samples) {
        double sum=0,square=0,dot=0;
        for(int i=0;i<samples.X.Length;i++) {
            double value=image[(y+samples.Y[i])*stride+x+samples.X[i]];
            sum+=value;square+=value*value;dot+=value*samples.Centered[i];
        }
        double variance=square-sum*sum/samples.X.Length;
        // Flat areas are not evidence, even if floating point noise correlates.
        if(variance<samples.X.Length*25 || samples.Norm<1) return -1;
        return Math.Max(-1,Math.Min(1,dot/(Math.Sqrt(variance)*samples.Norm)));
    }
    static bool SamePosition(Match a,Match b) {
        double dx=(a.X+a.Width*.5)-(b.X+b.Width*.5);
        double dy=(a.Y+a.Height*.5)-(b.Y+b.Height*.5);
        double radius=Math.Max(8,Math.Min(a.Width,a.Height)*.65);
        return dx*dx+dy*dy<radius*radius;
    }
    static void KeepPeak(List<Match> peaks,Match candidate) {
        for(int i=0;i<peaks.Count;i++) if(SamePosition(peaks[i],candidate)) {
            if(candidate.Score>peaks[i].Score) peaks[i]=candidate;
            return;
        }
        peaks.Add(candidate);
        peaks.Sort(delegate(Match a,Match b) { return b.Score.CompareTo(a.Score); });
        if(peaks.Count>8) peaks.RemoveAt(peaks.Count-1);
    }
    public static Match Find(Bitmap toolbar,Bitmap original) {
        if(toolbar.Width<32 || toolbar.Width>4096 || toolbar.Height<24 || toolbar.Height>160 ||
           original.Width!=142 || original.Height!=130) throw new Exception("ICON_IMAGE_INVALID");
        Stopwatch clock=Stopwatch.StartNew();
        double[] pixels=Gray(toolbar);
        List<Match> refined=new List<Match>();
        Color background=toolbar.GetPixel(toolbar.Width-2,toolbar.Height/2);
        int low=Math.Max(20,(int)Math.Round(toolbar.Height*.45));
        int high=Math.Min(112,(int)Math.Round(toolbar.Height*.85));
        // Test every integer height: a two-pixel step omits half the scales.
        // The same five-second budget still bounds the additional work.
        for(int h=low;h<=high;h++) {
            int w=(int)Math.Round(h*142.0/130);
            int px=(int)(w*.16),py=(int)(h*.12);
            int tw=w-2*px,th=h-2*py;
            if(tw>toolbar.Width || th>toolbar.Height) continue;
            using(Bitmap scaled=new Bitmap(w,h,PixelFormat.Format32bppArgb)) {
                using(Graphics g=Graphics.FromImage(scaled)) {
                    g.Clear(background);g.InterpolationMode=InterpolationMode.HighQualityBicubic;
                    using(ImageAttributes attrs=new ImageAttributes()) {
                        attrs.SetWrapMode(WrapMode.TileFlipXY);
                        g.DrawImage(original,new Rectangle(0,0,w,h),0,0,original.Width,original.Height,GraphicsUnit.Pixel,attrs);
                    }
                }
                using(Bitmap cropped=scaled.Clone(new Rectangle(px,py,tw,th),PixelFormat.Format32bppArgb)) {
                    double[] template=Gray(cropped);
                    Samples coarse=Sample(template,tw,th,2),full=Sample(template,tw,th,1);
                    List<Match> peaks=new List<Match>();
                    for(int y=0;y<=toolbar.Height-th;y+=2) {
                        if(clock.ElapsedMilliseconds>5000) throw new Exception("ICON_MATCH_TIMEOUT");
                        for(int x=0;x<=toolbar.Width-tw;x+=3) {
                            double score=Score(pixels,toolbar.Width,x,y,coarse);
                            if(score>=.55) KeepPeak(peaks,new Match { X=x,Y=y,Width=tw,Height=th,ScaleHeight=h,Score=score });
                        }
                    }
                    foreach(Match peak in peaks) {
                        Match best=null;
                        for(int y=Math.Max(0,peak.Y-2);y<=Math.Min(toolbar.Height-th,peak.Y+2);y++)
                            for(int x=Math.Max(0,peak.X-3);x<=Math.Min(toolbar.Width-tw,peak.X+3);x++) {
                                double score=Score(pixels,toolbar.Width,x,y,full);
                                if(best==null || score>best.Score)
                                    best=new Match { X=x,Y=y,Width=tw,Height=th,ScaleHeight=h,Score=score };
                            }
                        if(best!=null) refined.Add(best);
                    }
                }
            }
        }
        if(clock.ElapsedMilliseconds>5000) throw new Exception("ICON_MATCH_TIMEOUT");
        if(refined.Count==0) throw new Exception("ICON_TARGET_NOT_FOUND");
        refined.Sort(delegate(Match a,Match b) { return b.Score.CompareTo(a.Score); });
        Match winner=refined[0];double runner=-1;
        foreach(Match other in refined) if(!SamePosition(winner,other)) { runner=other.Score;break; }
        winner.RunnerUp=runner;winner.ElapsedMs=clock.ElapsedMilliseconds;
        // The Python caller repeats these gates and rejects out-of-bounds results.
        return winner;
    }
}
