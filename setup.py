from setuptools import setup, find_packages
import os

version = '1.0'

setup(name='plone.dexterity',
      version=version,
      description="Flexible CMF content",
      long_description=open("README.txt").read() + "\n" +
                       open(os.path.join("docs", "HISTORY.txt")).read(),
      # Get more strings from http://www.python.org/pypi?%3Aaction=list_classifiers
      classifiers=[
        "Framework :: Plone",
        "Programming Language :: Python",
        "Topic :: Software Development :: Libraries :: Python Modules",
        ],
      keywords='',
      author='Martin Aspeli',
      author_email='optilude@gmail.com',
      url='http://code.google.com/p/dexterity',
      license='GPL version 2',
      packages=find_packages(exclude=['ez_setup']),
      namespace_packages=['plone'],
      include_package_data=True,
      message_extractors = {"plone": [
            ("**.py",    "chameleon_python", None),
            ("**.pt"  ,  "chameleon_xml", None),
            ]},
      zip_safe=False,
      install_requires=[
          # 'Acquisition',
          # 'AccessControl',
      
          'plone.alterego',
          'plone.autoform>=1.0b2',
          'plone.behavior>=1.0b5',
          'plone.folder',
          'plone.memoize',
          'plone.rfc822',
          'plone.supermodel>=1.0b2',
          'plone.synchronize',
          'plone.z3cform>=0.6.0',
          'Products.CMFCore',
          'Products.CMFDefault',
          'Products.CMFDynamicViewFTI',
          'Products.statusmessages',
          'rwproperty',
          'setuptools',
          'Zope2',
          'zope.interface',
          'zope.component',
          'zope.container',
          'zope.dottedname',
          'zope.schema',
          'zope.lifecycleevent',
          'zope.location',
          'zope.dottedname',
          'zope.annotation',
          'zope.publisher',
          'zope.deferredimport',
          'zope.security',
          'zope.app.content',
          'zope.filerepresentation>=3.6.0',
          'zope.size',
          'ZODB3',
      ],
      extras_require={
        'test': ['plone.mocktestcase>=1.0b3',]
      },
      entry_points="""
      # -*- Entry points: -*-
      """,
      )
