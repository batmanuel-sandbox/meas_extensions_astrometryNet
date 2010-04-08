import math, os, sys
import eups
import lsst.pex.policy as policy
import lsst.afw.detection as afwDetection
import lsst.afw.image as afwImage
import lsst.afw.math as afwMath
import lsst.afw.display.ds9 as ds9
import lsst.meas.algorithms as algorithms

try:
    type(display)
except NameError:
    display = False

def detectFootprints(exposure, positiveThreshold, psf=None, negativeThreshold=None, npixMin=1):
    """Detect sources above positiveThreshold in the provided exposure returning the
    detectionSet.  Only sources with at least npixMin are considered

    If negativeThreshold, return a pair of detectionSets, dsPositive, dsNegative
    """

    assert positiveThreshold or negativeThreshold # or both

    if positiveThreshold:
        positiveThreshold = afwDetection.Threshold(positiveThreshold)
    if negativeThreshold:
        negativeThreshold = afwDetection.Threshold(negativeThreshold)
    #
    # Unpack variables
    #
    maskedImage = exposure.getMaskedImage()

    if not psf:                         # no need to convolve if we don't know the PSF
        convolvedImage = maskedImage
        llc = afwImage.PointI(0, 0)
        urc = afwImage.PointI(maskedImage.getWidth() - 1, maskedImage.getHeight() - 1)
    else:
        convolvedImage = maskedImage.Factory(maskedImage.getDimensions())
        convolvedImage.setXY0(maskedImage.getXY0())
        
        if display:
            ds9.mtv(maskedImage)
        # 
        # Smooth the Image
        #
        psf.convolve(convolvedImage, 
                     maskedImage, 
                     convolvedImage.getMask().getMaskPlane("EDGE"))
        #
        # Only search psf-smooth part of frame
        #
        llc = afwImage.PointI(psf.getKernel().getWidth()/2, 
                            psf.getKernel().getHeight()/2)
        urc = afwImage.PointI(convolvedImage.getWidth() - 1, convolvedImage.getHeight() - 1)
        urc -= llc

    bbox = afwImage.BBox(llc, urc)
    middle = convolvedImage.Factory(convolvedImage, bbox)

    dsNegative = None 
    if negativeThreshold != None:
        #detect negative sources
        dsNegative = afwDetection.makeDetectionSet(middle, negativeThreshold, "DETECTED_NEGATIVE", npixMin)
        if not ds9.getMaskPlaneColor("DETECTED_NEGATIVE"):
            ds9.setMaskPlaneColor("DETECTED_NEGATIVE", ds9.CYAN)

    dsPositive = None
    if positiveThreshold != None:
        dsPositive = afwDetection.makeFootprintSet(middle, positiveThreshold, "DETECTED", npixMin)
    #
    # ds only searched the middle but it belongs to the entire MaskedImage
    #
    dsPositive.setRegion(afwImage.BBox(afwImage.PointI(maskedImage.getX0(), maskedImage.getY0()),
                                       maskedImage.getWidth(), maskedImage.getHeight()));
    if dsNegative:
        dsNegative.setRegion(afwImage.BBox(afwImage.PointI(maskedImage.getX0(), maskedImage.getY0()),
                                           maskedImage.getWidth(), maskedImage.getHeight()));
    #
    # We want to grow the detections into the edge by at least one pixel so that it sees the EDGE bit
    #
    grow, isotropic = 1, False
    dsPositive = afwDetection.FootprintSetF(dsPositive, grow, isotropic)
    dsPositive.setMask(maskedImage.getMask(), "DETECTED")

    if dsNegative:
        dsNegative = afwDetection.FootprintSetF(dsNegative, grow, isotropic)
        dsNegative.setMask(maskedImage.getMask(), "DETECTED_NEGATIVE")
    #
    # clean up
    #
    del middle

    if negativeThreshold:
        if positiveThreshold:
            return dsPositive, dsNegative
        return dsPositive, dsNegative
    else:
        return dsPositive

def detectSources(exposure, threshold, psf=None):
    """Detect sources above positiveThreshold in the provided exposure returning the sourceList
    """

    if not psf:
        FWHM = 5
        psf = algorithms.createPSF("DoubleGaussian", 15, 15, FWHM/(2*math.sqrt(2*math.log(2))))

    #
    # Subtract background
    #
    mi = exposure.getMaskedImage()
    bctrl = afwMath.BackgroundControl(afwMath.NATURAL_SPLINE);
    bctrl.setNxSample(int(mi.getWidth()/256) + 1);
    bctrl.setNySample(int(mi.getHeight()/256) + 1);
    backobj = afwMath.makeBackground(mi.getImage(), bctrl)

    img = mi.getImage(); img -= backobj.getImageF(); del img

    if display:
        ds9.mtv(exposure)

    ds = detectFootprints(exposure, threshold)

    objects = ds.getFootprints()
    #
    # Time to actually measure
    #
    moPolicy = policy.Policy.createPolicy(os.path.join(eups.productDir("meas_pipeline"),
                                                       "policy", "MeasureSources.paf"))
    moPolicy = moPolicy.getPolicy("measureObjects")

    measureSources = algorithms.makeMeasureSources(exposure, moPolicy, psf)

    sourceList = afwDetection.SourceSet()
    for i in range(len(objects)):
        source = afwDetection.Source()
        sourceList.append(source)

        source.setId(i)
        source.setFlagForDetection(source.getFlagForDetection() | algorithms.Flags.BINNED1);

        try:
            measureSources.apply(source, objects[i])
        except Exception, e:
            #print e
            pass

        if source.getFlagForDetection() & algorithms.Flags.EDGE:
            continue

        if display:
            xc, yc = source.getXAstrom() - mi.getX0(), source.getYAstrom() - mi.getY0()
            if False:
                ds9.dot("%.1f %d" % (source.getPsfFlux(), source.getId()), xc, yc+1)

            ds9.dot("+", xc, yc, size=1)

    return sourceList

def mergeSourceSets(sourceSetList):
    """Return the union of all the SourceSet in sourceSetList"""
    outlist = afwDetection.SourceSet()
    for sl in sourceSetList:
        for s in sl:
            outlist.append(s)

    return outlist

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

def makeSourceList(dir, basename, e, c, aList, threshold, verbose=0):
    """Return the sources detected above threshold in all the amplifiers, aList, of the given field

    E.g. sl = makeSourceList("/lsst/DC3root/rlp1173", "v704897", 0, 3, range(8), 500)
    """

    try:
        aList[0]
    except TypeError:
        aList = [aList]

    sourceSets = []
    for a in aList:
        filename = "%s-e%d-c%03d-a%02d.sci" % (basename, e, c, a)
        if dir:
            filename = os.path.join(dir, "IPSD", "output", "sci", "%s-e%d" % (basename, e), filename)

        if verbose:
            print filename

        exp = afwImage.ExposureF(filename)

        sourceSets.append(detectSources(exp, threshold))

    return mergeSourceSets(sourceSets)

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

def makeCcdMosaic(dir, basename, e, c, aList, imageFactory=afwImage.MaskedImageF, verbose=0):
    """Return an image of all the specified amplifiers, aList, for the given CCD

    E.g. sl = makeCcdMosaic("/lsst/DC3root/rlp1173", "v704897", 0, 3, range(8))
    """

    try:
        aList[0]
    except TypeError:
        aList = [aList]

    for what in ("header", "data"):
        if what == "header":
            bbox = afwImage.BBox()
            ampBBox = {}
            wcs = {}
        else:
            ccdImage = imageFactory(bbox.getWidth(), bbox.getHeight())
            ccdImage.set(0)
            ccdImage.setXY0(bbox.getLLC())

        for a in aList:
            filename = os.path.join(dir, "IPSD", "output", "sci", "%s-e%d" % (basename, e),
                                    "%s-e%d-c%03d-a%02d.sci" % (basename, e, c, a))
            if verbose and what == "header":
                print filename

            if what == "header":
                md = afwImage.readMetadata(filename + "_img.fits")
                xy0 = afwImage.PointI(md.get("CRVAL1A"), md.get("CRVAL2A"))
                xy1 = xy0 + afwImage.PointI(md.get("NAXIS1") - 1, md.get("NAXIS2") - 1)
                bbox.grow(xy0)
                bbox.grow(xy1)

                ampBBox[a] = afwImage.BBox(xy0, xy1)
                wcs[a] = afwImage.Wcs(md)
            else:
                try:
                    data = imageFactory(filename + "_img.fits")
                except:
                    data = imageFactory(filename)
                    
                ampImage = ccdImage.Factory(ccdImage, ampBBox[a])
                ampImage <<= data
                del ampImage

    try:
        ccdImage.getMask()
        if wcs.has_key(0):
            ccdImage = afwImage.ExposureF(ccdImage, wcs[0])
    except AttributeError:
        pass

    return ccdImage

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

def readStandards(filename):
    fd = file(filename, "r")

    sourceSet = afwDetection.SourceSet()
    lineno = 0
    for line in fd.readlines():
        lineno += 1
        try:
            id, flags, ra, dec, cts = line.split()
        except Exception, e:
            print "Line %d: %s: %s" % (lineno, e, line),

        s = afwDetection.Source()
        sourceSet.append(s)

        s.setId(int(id))
        s.setFlagForDetection(int(flags))
        s.setRa(float(ra))
        s.setDec(float(dec))
        s.setPsfFlux(float(cts))

    return sourceSet

def showStandards(standardStarSet, exp, frame, countsMin=None, flagMask=None, rmsMax=None, ctype=ds9.RED):
    """Show all the standards that are visible on this exposure

    If countsMin is not None, only show brighter Sources

    If flagMask is not None, ignore sources that have (flags & ~flagMask) != 0
    """

    wcs = exp.getWcs()
    width, height = exp.getMaskedImage().getWidth(), exp.getMaskedImage().getHeight()
    
    for s in standardStarSet:
        ra, dec = s.getRa(), s.getDec()
        x, y = wcs.raDecToXY(ra, dec)

        if x < 0 or x >= width or y < 0 or y >= height:
            continue

        counts = s.getPsfFlux()

        if counts < countsMin and countsMin is not None:
            continue

        if flagMask is not None:
            if (s.getFlagForDetection() & ~flagMask) != 0:
                continue

        rms = math.sqrt(s.getIxx() + s.getIyy())
        if rmsMax is not None and rms > rmsMax:
            continue

        if False:
            pt = "%.1f" % (rms)
        else:
            pt = "+"
        ds9.dot(pt, x, y, frame=frame, ctype=ctype)

def setRaDec(wcs, sourceSet):
    """Set the ra/dec fields in a sourceSet from [XY]Astrom"""
    
    for s in sourceSet:
        ra, dec = wcs.xyToRaDec(s.getXAstrom(), s.getYAstrom())
        s.setRa(ra)
        s.setDec(dec)

def writeSourceSet(sourceSet, outfile="-"):
    if outfile == "-":
        fd = sys.stdout
    else:
        fd = open(outfile, "w")
    
    for s in sourceSet:
        print >> fd, s.getId(), s.getXAstrom(), s.getYAstrom(), s.getRa(), s.getDec(), s.getPsfFlux(), s.getFlagForDetection()

def readSourceSet(fileName):
    fd = open(fileName, "r")

    sourceSet = afwDetection.SourceSet()
    lineno = 0
    for line in fd.readlines():
        lineno += 1
        try:
            id, x, y, ra, dec, cts, flags = line.split()
        except Exception, e:
            print "Line %d: %s: %s" % (lineno, e, line),

        s = afwDetection.Source()
        sourceSet.append(s)

        s.setId(int(id))
        s.setFlagForDetection(int(flags))
        s.setRa(float(ra))
        s.setXAstrom(float(x))
        s.setYAstrom(float(y))
        s.setDec(float(dec))
        s.setPsfFlux(float(cts))

    return sourceSet
