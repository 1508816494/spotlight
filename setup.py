from setuptools import setup


setup(
    name='spotlight',
    version='0.1.0',
    requirements=['numpy',
                  'scipy',
                  'h5py',
                  'pytorch==0.1.11',
                  'requests'],
    packages=['spotlight'],
    license='MIT',
    classifiers=['Development Status :: 3 - Alpha',
                 'License :: OSI Approved :: MIT License',
                 'Topic :: Scientific/Engineering :: Artificial Intelligence'],
)
